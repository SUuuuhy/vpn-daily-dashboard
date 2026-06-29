# GitHub 更新步骤：v3 严格时效 + 5 类面板

这次更新主要解决四个问题：

1. 每条信息必须与当前时间一致，默认只展示最近 36 小时内且能确认发布时间的信息。
2. 旧信息、无发布时间的信息，不再进入主面板。
3. 多个信息源反映同一件事时，会合并成一个统一要点，并在要点后附全部原帖 / 原文链接。
4. 面板只保留 5 类：竞品动态、社交媒体、reddit讨论、政策风险、第三方网站。

## 第 1 步：解压新版 ZIP

下载并解压新版包，进入文件夹后应看到：

```text
.github
config
docs
scripts
requirements.txt
README_无代码部署.md
CHECKLIST_只照做.md
CHANGELOG_V3_CN.md
GITHUB_UPDATE_V3_CN.md
```

## 第 2 步：用 GitHub Desktop 打开你的仓库

打开 GitHub Desktop，选择你的日报仓库。

点：

```text
Repository → Show in Finder
```

Windows 上是：

```text
Repository → Show in Explorer
```

这会打开你电脑里的仓库文件夹。

## 第 3 步：复制覆盖

把新版包里的这些内容复制到你的仓库根目录，选择覆盖旧文件：

```text
.github
config
docs
scripts
requirements.txt
README_无代码部署.md
CHECKLIST_只照做.md
CHANGELOG_V3_CN.md
GITHUB_UPDATE_V3_CN.md
```

正确结构应该是：

```text
你的仓库
├── .github
├── config
├── docs
├── scripts
├── requirements.txt
├── README_无代码部署.md
├── CHECKLIST_只照做.md
├── CHANGELOG_V3_CN.md
└── GITHUB_UPDATE_V3_CN.md
```

不要变成：

```text
你的仓库
└── vpn_daily_dashboard_auto_v3
    ├── .github
    ├── config
    ├── docs
    └── scripts
```

## 第 4 步：Commit 和 Push

回到 GitHub Desktop。

左下角提交信息写：

```text
update dashboard v3 strict freshness five categories
```

然后点：

```text
Commit to main
```

再点：

```text
Push origin
```

## 第 5 步：手动运行一次

回到 GitHub 网页版仓库。

点：

```text
Actions → Daily VPN Dashboard Update → Run workflow
```

Branch 选：

```text
main
```

再点绿色的：

```text
Run workflow
```

等它变成绿色对号。

## 第 6 步：检查 GitHub Pages

进入：

```text
Settings → Pages
```

确认：

```text
Source: GitHub Actions
```

如果不是，就改成 GitHub Actions。

## 第 7 步：打开网页检查

新版页面顶部会显示：

```text
Daily Source Intelligence Panel · v3
```

页面只保留：

```text
竞品动态
社交媒体
reddit讨论
政策风险
第三方网站
```

## 可选：调整时效窗口

默认只展示最近 36 小时。

要改时效窗口，打开：

```text
.github/workflows/daily-update.yml
```

找到：

```yaml
DASHBOARD_LOOKBACK_HOURS: "36"
FRESHNESS_HOURS: "36"
FRESHNESS_WINDOW_HOURS: "36"
```

如果想只看最近 24 小时，就把三个数字都改成：

```yaml
"24"
```

然后 Commit、Push、Run workflow。
