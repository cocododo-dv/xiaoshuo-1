"""数据库层：``base``（``Base``）、``models``（全部表）、``session``（引擎与会话）。

包本身不预先导入子模块（B12-20）：只要引擎的代码（工具、会话守卫）不必连带导入 70 多张表；
要用 ``Base.metadata`` 看全部表的代码自己 ``from novel_system.db import models``。
"""
