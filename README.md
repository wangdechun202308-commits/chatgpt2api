这个版本，个人做了一些改进，将文本模拟工具改为本地执行，仅供个人测试使用。

任何人不得恶意使用，不得违反 OpenAI 的各项规则。

## 简单使用方法

### 1. 下载稳定版

```bash
git clone https://github.com/wangdechun202308-commits/chatgpt2api.git
cd chatgpt2api

git checkout myaiagent-v4-stable
```

### 2. 按原 chatgpt2api 的方法完成配置并启动

先按照原项目说明配置自己的环境和账号，然后启动 `chatgpt2api`。

确认容器已经运行：

```bash
docker ps
```

应当能够看到：

```text
chatgpt2api
```

### 3. 安装 V4

在 `chatgpt2api` 项目目录执行：

```bash
docker cp \
  services/protocol/openai_v1_chat_complete.py \
  chatgpt2api:/app/services/protocol/openai_v1_chat_complete.py
```

检查 Python 语法：

```bash
docker exec chatgpt2api \
  /app/.venv/bin/python \
  -m py_compile \
  /app/services/protocol/openai_v1_chat_complete.py
```

然后重新启动：

```bash
docker restart chatgpt2api
```

### 4. 检查是否为稳定 V4

```bash
docker exec chatgpt2api \
  sha256sum \
  /app/services/protocol/openai_v1_chat_complete.py
```

正确的 V4 SHA256 为：

```text
3509005c054723b46af9092ec4c048518b724f3aed72dea6683cc4cae208308c
```

完全一致，即表示 V4 已正确安装。

稳定版本：

```text
Tag: myaiagent-v4-stable
```
