# sky-checkin

`sky.skyxing.cn` 多账号自动签到，两套运行方式共用同一个脚本。

## GitHub Actions

1. Settings → Secrets and variables → Actions → New repository secret
2. 名字 `SKY_ACCOUNTS`，值形如 `user1|pass1|备注1;user2|pass2`
3. Actions → Sky Check-in → Run workflow

定时：每天北京时间 08:10（cron `10 0 * * *` 是 UTC）。

> 注意：GitHub 的 schedule 是 best-effort，常延迟 5~20 分钟；
> 仓库 60 天无活动会自动停用定时任务。

## 青龙

环境变量 `SKY_ACCOUNTS` 同上，脚本放 `/ql/data/scripts/`，
定时规则 `0 8 * * *`，命令 `task sky-checkin.py`。

## 状态

- 登录判据：响应体 `ok: true` **且**拿到会话 cookie（服务端对失败请求也会发游客 cookie）
- 双入口：域名 → 直连 IP（手写 Host 头 + 跳过证书校验）
- 站点有限流，别连续狂跑
