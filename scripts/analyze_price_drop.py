"""分析: 被价格暴跌(>90%)淘汰的代币, 区分毕业/未毕业, 看是否有误杀"""
import json, os, glob
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# 收集所有被价格暴跌淘汰的代币, 以及它们在队列中的历史快照
price_drop_eliminated = {}
queue_history = defaultdict(list)  # addr -> [(scan_time, snapshot)]

files = sorted(glob.glob(os.path.join(DATA_DIR, "2026-*.json")))

for fpath in files:
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    scan_time = data.get("scanTime", "")
    
    for t in data.get("queue", []):
        addr = t.get("address", "")
        queue_history[addr].append((scan_time, {
            "holders": t.get("holders", 0),
            "peak_holders": t.get("peak_holders", 0),
            "progress": t.get("progress", 0),
            "price": t.get("price", 0),
            "peak_price": t.get("peak_price", 0),
            "age_hours": t.get("age_hours", 0),
        }))
    
    for t in data.get("eliminatedThisRound", []):
        reason = t.get("reason", "")
        if "价格跌" in reason:
            addr = t.get("address", "")
            price_drop_eliminated[addr] = {**t, "_scanTime": scan_time}

print(f"被价格暴跌淘汰的代币: {len(price_drop_eliminated)} 个\n")

for addr, t in sorted(price_drop_eliminated.items(), 
                       key=lambda x: x[1].get("peak_holders", 0) or 0, reverse=True):
    progress = 0
    is_graduated = False
    # 从队列历史中找到该代币的进度信息
    history = queue_history.get(addr, [])
    if history:
        last_snap = history[-1][1]
        progress = last_snap.get("progress", 0)
        is_graduated = progress >= 1.0
    
    grad_label = "已毕业" if is_graduated else f"未毕业({progress*100:.1f}%)"
    peak_h = 0
    if history:
        peak_h = max(s[1].get("peak_holders", 0) for s in history)
    
    # 价格变化轨迹
    prices = [s[1]["price"] for s in history if s[1]["price"] > 0]
    price_trajectory = ""
    if len(prices) >= 3:
        # 取前中后三个点
        p_start = prices[0]
        p_mid = prices[len(prices)//2]
        p_end = prices[-1]
        price_trajectory = f" 轨迹: {p_start:.2e}→{p_mid:.2e}→{p_end:.2e}"
    
    print(f"  {t.get('name', '')[:25]:25s} | {grad_label:15s} | 峰值持币:{peak_h:4d} 现:{t.get('holders', 0):4d} | "
          f"{t.get('reason', '')}{price_trajectory}")
    
    # 如果有历史, 看淘汰后这个代币是否在后续扫描的队列中出现过 (说明被重新发现)
    elim_time = t.get("_scanTime", "")
    reappeared = False
    for fpath2 in files:
        with open(fpath2, "r", encoding="utf-8") as f2:
            d2 = json.load(f2)
        if d2.get("scanTime", "") > elim_time:
            for qt in d2.get("queue", []):
                if qt.get("address") == addr:
                    reappeared = True
                    break
        if reappeared:
            break
    if reappeared:
        print(f"  {'':25s}   ⚠️ 淘汰后重新出现在队列中!")
