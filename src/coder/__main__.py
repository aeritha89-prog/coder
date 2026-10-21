"""支持通过 ``python -m coder`` 进入命令行。"""

from .cli import main

# 将 main 的退出码交给操作系统，便于脚本判断任务是否完成。
raise SystemExit(main())
