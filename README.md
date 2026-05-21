# 13F Holdings Change Pusher

追踪 5 家机构的 SEC 13F 持仓变化，并把新增、加仓、减仓、清仓整理后推送到 Discord。

## 当前追踪机构

- Berkshire Hathaway, CIK `0001067983`
- Bridgewater Associates, CIK `0001350694`
- Soros Fund Management, CIK `0001029160`
- Appaloosa, CIK `0001656456`
- Situational Awareness LP, CIK `0002045724`

`Situational Awareness LP` 是 Leopold Aschenbrenner 相关 13F 申报主体。

## 本地运行

```powershell
py tg_push.py --status
py tg_push.py --preview 1 --dry-run
py tg_push.py
```

测试时务必使用 `--dry-run`，这样只打印消息，不会真的发到 Discord。

## Discord 推送

在 Discord 频道创建 Webhook 后，配置：

```powershell
$env:DISCORD_WEBHOOK_URL="你的 Discord webhook URL"
$env:SEC_USER_AGENT="your-name your-email@example.com"
py tg_push.py
```

## State 持久化配置

GitHub Actions 每次运行都是 fresh run，本地文件可能丢失。脚本支持把 state 写入 GitHub Gist，避免同一份 13F 重复推送。

### 新建空 Gist

1. 打开 <https://gist.github.com/>
2. 新建一个 Gist
3. 文件名填 `state.json`
4. 内容填：

```json
{}
```

5. 创建后复制 URL 里的 Gist ID，例如：

```text
https://gist.github.com/your-name/abcdef1234567890
```

这里的 `abcdef1234567890` 就是 `GITHUB_GIST_ID`。

### GitHub Repo Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 添加：

- `DISCORD_WEBHOOK_URL`
- `GITHUB_GIST_ID`
- `SEC_USER_AGENT`
- 可选：`TG_BOT_TOKEN`
- 可选：`TG_CHAT_ID`

Workflow 会把内置 `GITHUB_TOKEN` 传给脚本。如果你的 Gist 无法被内置 token 更新，请改用带 `gist` 权限的 GitHub token，并作为 workflow 环境变量 `GITHUB_TOKEN` 传入。

Gist 中的 `state.json` 会保持类似格式：

```json
{
  "0001067983": {
    "last_report_date": "2026-03-31",
    "last_filing_date": "2026-05-15"
  }
}
```

如果 Gist 不可用，脚本会自动 fallback 到本地 `data/holdings_state.json`，不中断运行。

## 推送效果预览

```text
📊 **Berkshire Hathaway** 持仓变化
报告期：2026-03-31 | 对比：2025-12-31
总持仓：$267.31B → $263.10B（变化 -$4.21B）

🆕 新增（3支）
- DELTA AIR LINES INC (CUSIP 247361702)：39,809,456 股 | $2.65B | 占新总仓 1.0%

📈 加仓（4支，按变化市值排序）
- ALPHABET INC (CUSIP 02079K305)：+36,403,656 股 (+204.0%) | 市值变化 +$10.01B

📉 减仓（6支，按变化市值排序）
- CHEVRON CORPORATION (CUSIP 166764100)：-45,780,506 股 (-35.2%) | 市值变化 -$2.38B

❌ 清仓（16支）
- VISA INC (CUSIP 92826C839)：8,297,460 股 | $2.91B | 占新总仓 1.1%

⚪ 股数不变（10支，市值变化 > $100M）
- APPLE INC (CUSIP 037833100)：227,917,808 股 | 市值变化 -$4.12B
```

## 季度汇总

每次检测到新 13F 后，如果 5 家机构中已有至少 3 家提交了同一报告期的 13F，会额外推送季度汇总：

```text
🗓 **Q1 2026 机构持仓季度汇总**
已收录：5/5 家

| 机构 | 总仓位 | 环比变化 | 最大加仓 | 最大减仓 |
|------|--------|----------|----------|----------|
| 伯克希尔 | $263.10B | -$4.21B | ALPHABET INC | CHEVRON CORPORATION |

共同新增：AAA、BBB
共同清仓：CCC
```

## 自动运行频率

推荐保留当前 GitHub Actions 配置：工作日每 6 小时运行一次。

13F 是季度披露，通常在季度结束后最多 45 天内提交。脚本只有发现新报告期时才推送，所以每 6 小时检查一次不会刷屏。
