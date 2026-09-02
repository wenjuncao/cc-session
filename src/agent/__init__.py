# -*- coding: utf-8 -*-
"""
agent —— cc-session 被控端子包（自成一体）。

把 remote-disp 的“被控”能力（发现广播、握手鉴权、看文件目录、下载文件、截屏、看屏幕、进程查看/关闭）
以后台守护线程形式并入 cc-session，与 tkinter 界面完全解耦：无任何网页 UI。
对外仅暴露 start.start_background() / start.run_silent() 及 lockservice 的服务安装接口。
"""
