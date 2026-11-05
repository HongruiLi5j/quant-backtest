"""环境守卫：测试依赖的外部目录 / 本地行情数据不在时，明确标成「跳过」，而不是报「失败」。

用法：
    from env_guard import skip_unless
    @skip_unless(Path('...').exists(), '缺少 xxx：说明缺的是什么')
    def test_xxx(): ...

跳过只用于「环境缺东西」（干净副本里没有 ../ai_layer、没有某只股票的本地日线），不是用来掩盖代码错误：
  - 条件只看文件/目录是否存在，不捕获任何异常；东西在而测试失败，照样是失败。
  - 完整环境（站主本机、需要把关的 CI）设置 QB_REQUIRE_FULL_ENV=1 后，skip_unless 不再跳过：
    环境缺东西会直接失败，不会悄悄变成一片绿色的“跳过”。
  - pytest 加 -rs 能看到每一项被跳过的原因；脚本方式运行（python tests/xxx.py）会在汇总里列出。
"""
import os
import unittest

STRICT = os.environ.get('QB_REQUIRE_FULL_ENV') == '1'


def skip_unless(condition, reason):
    """condition 为假就跳过并写明原因；严格模式（QB_REQUIRE_FULL_ENV=1）下不跳过，让缺失直接暴露成失败。"""
    if STRICT:
        return lambda fn: fn
    return unittest.skipUnless(condition, reason)
