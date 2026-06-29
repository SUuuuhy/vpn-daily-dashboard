# 只照做清单

1. 下载并解压 `vpn_daily_dashboard_auto_v3.zip`。
2. 打开 GitHub Desktop。
3. 选择你的日报仓库。
4. 点 `Repository → Show in Finder` / `Show in Explorer`。
5. 把解压后 `vpn_daily_dashboard_auto_v3` 里面的内容复制到仓库根目录，选择覆盖。
6. 回到 GitHub Desktop。
7. 左下角提交信息写：

```text
update dashboard v3 strict freshness five categories
```

8. 点 `Commit to main`。
9. 点 `Push origin`。
10. 打开 GitHub 网页仓库。
11. 进入 `Actions → Daily VPN Dashboard Update`。
12. 点 `Run workflow`。
13. Branch 选 `main`。
14. 再点绿色 `Run workflow`。
15. 等绿色对号出现。
16. 打开你的 GitHub Pages 地址查看新版面板。

新版判断规则：

- 旧信息不会进入主面板。
- 没有发布时间的信息不会进入主面板。
- 默认只展示最近 36 小时的信息。
- 每个要点后都会有来源链接。
- 多个来源反映同一件事，会合并成一个要点。
