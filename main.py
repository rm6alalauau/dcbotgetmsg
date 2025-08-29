import discord
import os
import re
import json
import requests
from datetime import datetime
from dateutil.parser import parse, ParserError
import asyncio

# --- 讀取 GitHub Secrets ---
TARGET_CHANNEL_ID = 1260090030270185533
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
WORKER_UPLOAD_URL = os.environ.get("WORKER_UPLOAD_URL")
WORKER_SECRET_KEY = os.environ.get("WORKER_SECRET_KEY")

# --- Cloudflare Worker 相關函式 (保持不變) ---
def get_current_codes_from_worker():
    """從 Worker API 獲取當前的資料庫狀態，以便合併"""
    # 我們需要從上傳 URL 中移除 /upload 路徑
    api_url = WORKER_UPLOAD_URL.replace("/upload", "")
    try:
        response = requests.get(api_url)
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        print(f"從 Worker 獲取現有資料失敗: {e}，將從空資料庫開始。")
        return {} # 如果失敗，就從一個空的字典開始

def upload_via_worker(data):
    if not WORKER_UPLOAD_URL or not WORKER_SECRET_KEY:
        print("缺少 Worker URL 或 Secret Key，跳過上傳。")
        return
    headers = { 'Content-Type': 'application/json', 'X-Auth-Key': WORKER_SECRET_KEY }
    json_string_data = json.dumps(data, ensure_ascii=False)
    try:
        response = requests.post(WORKER_UPLOAD_URL, headers=headers, data=json_string_data.encode('utf-8'))
        response.raise_for_status()
        print(f"成功透過 Worker 上傳資料。Worker 回應: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"透過 Worker 上傳失敗: {e}")

# --- 您完美的解析器 (保持不變) ---
def _parse_data_line(data_line):
    """
    【V10 最終正確版】
    - 根據使用者的關鍵發現，優先處理 Discord 的 Unix 時間戳格式 <t:1756051199:F>。
    - 這將是最穩定和最準確的方法。
    """
    rewards = []
    expiry_info = "未知"

    # 1. 【核心修正】優先尋找並解析 Discord 的 Unix 時間戳格式
    #    正規表示式 r'<t:(\d+):?[a-zA-Z]?>' 用於抓取 <t:...> 中間的數字
    expiry_ts_match = re.search(r'<t:(\d+):?[a-zA-Z]?>', data_line)

    if expiry_ts_match:
        try:
            # 提取 group(1) 捕獲到的數字字串，也就是 Unix timestamp
            timestamp_str = expiry_ts_match.group(1)
            # 將字串轉換為整數
            timestamp_int = int(timestamp_str)
            # 使用 datetime.fromtimestamp() 將秒數轉換為本地時區的日期時間物件
            dt = datetime.fromtimestamp(timestamp_int)
            # 格式化成我們需要的 YYYY/M/D 格式
            expiry_info = f"{dt.year}/{dt.month}/{dt.day}"
        except (ValueError, TypeError):
            # 如果因為任何原因轉換失敗，則放棄
            expiry_info = "未知"

    # 2. 如果沒有找到時間戳，再檢查是否為 "版本結束" 的文字格式（作為備用方案）
    elif "版本結束" in data_line:
        version_match = re.search(r'到\s*([\d\.]+)\s*版本結束', data_line)
        expiry_info = f"到 {version_match.group(1)} 版本結束" if version_match else "版本結束"

    # 3. 再備用檢查純文字日期，以防萬一有手動輸入的日期
    elif '年' in data_line and '月' in data_line and '日' in data_line:
        expiry_str_match = re.search(r'(\d{4}年\s*\d{1,2}月\s*\d{1,2}日)',
                                     data_line)
        if expiry_str_match:
            # 因為 dateutil.parser 依然可能出錯，所以這裡簡化處理
            expiry_info = expiry_str_match.group(1).replace('年', '/').replace(
                '月', '/').replace('日', '')

    # 4. 提取並清洗獎勵 (這部分邏輯不變，依然有效)
    rewards_part = data_line
    if '截止時間' in data_line:
        rewards_part = data_line.split('截止時間')[0]

    cleaned_text = _remove_emojis(rewards_part)
    cleaned_text = re.sub(r'，\s*\*?先到先得\**', '', cleaned_text)
    cleaned_text = cleaned_text.strip('↑ ').rstrip('，,+')

    rewards = [r.strip() for r in cleaned_text.split('+') if r.strip()]

    return rewards, expiry_info

def parse_message_for_codes(content):
    """
    【V8 版主解析函式】
    - 修正了獨立兌換碼的正規表示式，使其支援大小寫字母。
    - 沿用 V6 的特殊格式處理邏輯。
    """
    final_data = {}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 1. 【關鍵修正】在正規表示式中加入 a-z，使其可以匹配小寫字母
    standalone_codes = re.findall(r'^[A-Za-z0-9]{6,}$', content, re.MULTILINE)
    url_codes = re.findall(r'redemption\?code=([A-Za-z0-9]+)',
                           content)  # 同步修改 URL 規則
    all_codes = sorted(list(set(standalone_codes + url_codes)))

    if not all_codes:
        return None

    # 2. 找到所有獎勵/資料行 (邏輯不變)
    data_lines = re.findall(r'^↑.*$', content, re.MULTILINE)

    # 3. 根據獎勵/資料行的數量決定解析策略 (此後邏輯與 V6/V7 相同，無需變更)

    # 策略一: 1:N 模式 (單一獎勵行對應多個兌換碼)
    if len(data_lines) <= 1:
        data_line_text = data_lines[0] if data_lines else "↑"
        rewards, expiry_info = _parse_data_line(data_line_text)

        if len(all_codes) > 1 and len(rewards) == 1 and 'x' in rewards[0]:
            match = re.search(r'(.+?)\s*x\s*(\d+)', rewards[0])
            if match and int(match.group(2)) == len(all_codes):
                base_reward = match.group(1).strip()
                rewards = [base_reward]

        for code in all_codes:
            final_data[code] = {
                'rewards': rewards,
                'expiry_info': expiry_info,
                'added_time': now_str
            }

    # 策略二: N:N 模式 (多個獎勵行對應各自的兌換碼)
    else:
        message_lines = content.splitlines()
        for i, line in enumerate(message_lines):
            if line.startswith('↑'):
                if i > 0 and re.fullmatch(r'^[A-Za-z0-9]{6,}$',
                                          message_lines[i - 1].strip()):
                    code = message_lines[i - 1].strip()
                    rewards, expiry_info = _parse_data_line(line)
                    final_data[code] = {
                        'rewards': rewards,
                        'expiry_info': expiry_info,
                        'added_time': now_str
                    }

    return final_data if final_data else None



# --- 主執行邏輯 ---
async def main():
    if not DISCORD_TOKEN:
        print("錯誤：找不到 DISCORD_TOKEN。")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f'機器人 {client.user} 已臨時登入。')
        
        all_codes = get_current_codes_from_worker()
        original_codes_count = len(all_codes)

        channel = client.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            print(f"錯誤：找不到頻道 ID {TARGET_CHANNEL_ID}。")
            await client.close()
            return
        
        print(f"正在從頻道 '{channel.name}' 讀取最新 10 條訊息...")
        async for message in channel.history(limit=10):
            new_data = parse_message_for_codes(message.content)
            if new_data:
                all_codes.update(new_data)

        if len(all_codes) > original_codes_count:
            print(f"發現新資料！總數從 {original_codes_count} 變為 {len(all_codes)}。正在上傳...")
            upload_via_worker(all_codes)
        else:
            print("未發現新資料，無需更新。")

        await client.close()
        print("任務完成，機器人已登出。")

    # <<<--- 核心修改點 ---
    try:
        # 用 asyncio.wait_for 包裹 client.start()，並設定 60 秒的超時
        print("正在嘗試連接到 Discord... (60秒超時)")
        await asyncio.wait_for(client.start(DISCORD_TOKEN), timeout=60.0)
    except asyncio.TimeoutError:
        print("錯誤：連接 Discord 超時 (超過 60 秒)。可能是 GitHub Actions 的網路問題。")
        # 即使超時，也確保客戶端被正確關閉
        await client.close()
    except Exception as e:
        print(f"運行時發生未預期的錯誤: {e}")
        await client.close()
    # <<<--------------------

# 運行主函式
if __name__ == "__main__":
    asyncio.run(main())
