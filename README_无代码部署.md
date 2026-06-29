# VPN 信息源观察日报 v3 无代码部署说明

这是一套 GitHub Pages + GitHub Actions 的自动日报网页。

## v3 的核心规则

- 只展示最近 36 小时内的信息。
- 每条信息必须有明确发布时间或更新时间。
- 旧信息、无日期信息、旧日报 seed 不会进入主面板。
- 页面只保留 5 类：竞品动态、社交媒体、reddit讨论、政策风险、第三方网站。
- 同一事件多来源重复出现时，会合并成一个要点，并列出全部原帖/来源链接。
- 不再输出增长运营行动建议。

## 目录说明

```text
.github/workflows/daily-update.yml   每日自动运行任务
config/sources.csv                   信息源池
config/manual_inputs.csv             手工补充入口
scripts/update_dashboard.py          自动抓取、过滤、聚合、生成网页脚本
docs/index.html                      GitHub Pages 展示页面
docs/data/latest.json                最新日报数据
docs/archive                         每日归档
docs/reports                         Markdown 日报归档
```

## 手工补充怎么用

如果某个来源自动抓不到，例如 Discord、TikTok、X 官方帖、商店评论，你可以编辑：

```text
config/manual_inputs.csv
```

填入：

```text
日期,来源,标题,摘要,链接,面板分类,子分类,重要性分,备注
```

`面板分类` 只能填：

```text
竞品动态
社交媒体
reddit讨论
政策风险
第三方网站
```

日期留空时，系统会按当天纳入。链接必须是准确原帖或原文链接。

## 调整时效窗口

默认只看最近 36 小时。要修改，打开：

```text
.github/workflows/daily-update.yml
```

找到：

```yaml
FRESHNESS_WINDOW_HOURS: "36"
```

改成你想要的小时数，例如只看最近 24 小时：

```yaml
FRESHNESS_WINDOW_HOURS: "24"
```

不建议超过 72 小时。

## Reddit 更稳定的配置

在 GitHub 仓库里进入：

```text
Settings → Secrets and variables → Actions → New repository secret
```

添加：

```text
REDDIT_CLIENT_ID
REDDIT_CLIENT_SECRET
REDDIT_USER_AGENT
```

## 官方社媒自动抓取

如果要自动抓取竞品官方 X/Twitter 账号，添加：

```text
X_BEARER_TOKEN
```

如果不配置，社交媒体分类不会乱抓旧网页；你可以用 `manual_inputs.csv` 手工补当天官方帖。
