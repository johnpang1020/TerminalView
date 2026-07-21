# 執行手冊（對齊 stock.py — 順勢 / MA / 再平衡）

> **舊伏擊規則已廢止**：Entry 658 / 0.80·0.70 伏擊線 / PE≤25 買入鎖 / 高於基準冰封  
> **一律以本檔 + `stock.py` 為準。**

## 月供（16 日合流）

總流出約 **9,000 HKD** = `8,000` 主體 + `1,000` BTC 額度。

| 部位 | 金額 | 規則 |
|------|------|------|
| 核心 70% | 5,600 HKD | 一律買 **SPYL.L** |
| 衛星 30%（相對 8,000） | 2,400 HKD | SMH 收盤 **≥ 60MA** → 買 **SMH**；否則 → **SPYL.L** |
| BTC 額度 | 1,000 HKD | BTC ≥ 60MA 才考慮買入；**補到總資產 5% 為止**（partial fill），剩餘 → SPYL |

- **PE / `valuation_safe`**：只寫日誌，**不閘控買入**。  
- **不再**把月供灌進 IB01/CASH 當伏擊彈藥。  
- IB01 = 短債停泊；舊倉可留，新月供預設不追加。

## 再平衡（檢查權重 → 指令，不下單）

| 觸發 | 動作 |
|------|------|
| SMH+BTC ≥ **40%** | 賣超額至衛星 **30%**（先 SMH 後 BTC），所得買 SPYL |
| BTC > **5%** | 賣超額 BTC → SPYL |
| CASH+IB01 > **10%** | **HINT**（非強制）：建議賣 IB01/CASH → SPYL |

## 儀表板（stock.py）

```text
python stock.py                  # dashboard + MA 分流 + 再平衡提示
python stock.py --allocate-only  # 只出執行 JSON
python stock.py --export-json    # 含 PE 觀察 CSV（仍不閘買入）
```

持倉市值：`portfolio_positions.json`（USD）  
鍵：`SPYL.L`, `SMH`, `BTC-USD`, `CASH`, `IB01.L`

## 信用警報

利差/速度觸發時：**只警報**。  
**不**自動賣 IB01、**不**自動買 SMH。必須再對照 60MA 與再平衡指令。

## 觀測指標（不決定買賣）

- SMH trailing PE / percentile（`smh_pe_history.csv`）  
- 記憶體剪刀差、SMH 健康 0/5、ICI/VOO/SPY-RSP 結構  

## 廢止清單（勿再執行）

- Entry Price / 第二·三段伏擊線機械掃貨  
- `valuation_safe=False` → 禁止買 SMH  
- 月供 1,400 冰封等砸坑  
- 信用訊號 → 賣短債滿倉 SMH  
