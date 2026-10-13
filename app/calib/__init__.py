"""app.calib：相机标定——名义标定生成、结果持久化、标定帧抽取、core 标定调用。

全部无 Qt 依赖，可无头测试。标定结果 JSON 结构对齐 batana-core 的
session-schema calibration 字段（format_version/stereo/extrinsic + report 小节）。
"""
