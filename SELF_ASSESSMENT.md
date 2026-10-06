# Self-assessment wording

## Agent automation ability

独立开发可复用的批量视频处理工具，自动读取 JSON 任务、执行 FFmpeg、校验输出，并在失败时切换参数重试两次。

## Automatic video-editing development

开发 AutoClip Batch 自动剪辑工具，可批量裁剪视频、转换 9:16 竖屏、添加标题或水印，并使用 ffprobe 验证导出结果。

## Token-saving awareness

将剪辑方案固化为可复用 JSON 配置，并使用 SHA-256 文件哈希缓存成功结果，通过确定性本地程序避免重复 Agent 调用和重复处理。

## Interview explanation

这个项目的循环是：读取任务、执行剪辑、检查输出、失败重试、成功后写入缓存并退出。循环具有明确的检查条件、最多两次重试和成功或失败终止条件。
