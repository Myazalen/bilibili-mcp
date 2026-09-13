#!/usr/bin/env python3
"""B站扫码登录，保存凭证供 MCP Server 使用"""

import asyncio
import json
from pathlib import Path
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

CRED_FILE = Path(__file__).parent / "bili_credential.json"

async def main():
    qr = QrCodeLogin()
    await qr.generate_qrcode()
    print(qr.get_qrcode_terminal())
    print("\n请用B站App扫描上方二维码（180秒内有效）\n")

    while True:
        state = await qr.check_state()
        if state == QrCodeLoginEvents.SCAN:
            print("✅ 已扫码，请在手机上确认...")
        elif state == QrCodeLoginEvents.CONF:
            print("✅ 已确认...")
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("❌ 二维码超时，请重新运行")
            return
        elif state == QrCodeLoginEvents.DONE:
            break
        await asyncio.sleep(2)

    cred = qr.get_credential()
    if not (cred.sessdata or "").strip():
        print("\n❌ 扫码已确认，但没拿到有效的 SESSDATA（bilibili-api 解析登录返回串失败），凭证未保存。")
        print("   请重新运行本脚本；若反复失败，可在 MCP 里用 bili_login_with_cookies 手动填入浏览器 Cookies。")
        return

    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "sessdata": cred.sessdata,
            "bili_jct": cred.bili_jct,
            "buvid3": cred.buvid3,
            "buvid4": cred.buvid4,
            "dedeuserid": cred.dedeuserid,
            "ac_time_value": cred.ac_time_value,
        }, f, ensure_ascii=False)
    print(f"\n🎉 登录成功！凭证已保存到 {CRED_FILE}")
    print("   建议再运行一次 MCP 的 bili_check_credential 确认登录态可用。")

if __name__ == "__main__":
    asyncio.run(main())
