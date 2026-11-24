# Muse Mail 收件监测 hook

每分钟查一次 Muse Mail 收件箱。用户已验证邮箱发来新邮件时，叫醒处理；没有新邮件就不出声，不花 token。

本仓库的 main 分支是微信桥接代码，本分支（mail）只放邮件监测代码，两边分开。

## 文件

- `scripts/muse-mail-inbox-watch.sh`：轮询脚本，只查不办。首次运行先把已有邮件记为已读，不唤醒；只认发件人是用户本人（OWNER）的收件箱邮件；可疑邮件（QUARANTINE / AUTHENTICATION_FAILURE）不触发。
- `definitions/muse-mail-inbox-watch.json`：hook 定义：60 秒一轮，投递主聊天，附 worker 处理指令。

长期处理规则（2026-11-24）：验证邮箱来信先回简短确认（已收到＋任务简述＋预计时间），办完直接把结果和附件发回发件人，不用聊天确认；只在任务说不清、需要拍板、发给别人时才问。

## 安装

1. 脚本放到 `~/hooks/scripts/`，定义放到 `~/hooks/definitions/`。
2. 注册 hook（id：`muse-mail-inbox-watch`），先 dry_run 试跑，再启用。
3. 首次运行会把现有邮件记为已读，不唤醒，是正常的。

## 更新办法

这里是推送用的源目录，正在跑的文件在 `~/hooks/`。更新时先把 `~/hooks/` 里的最新版拷过来，再推。
