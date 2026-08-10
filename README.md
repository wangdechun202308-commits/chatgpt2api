这个版本，个人做了一些改进，将文本模拟工具改为本地执行，仅供个人测试使用。

任何人不得恶意使用，不得违反 OpenAI 的各项规则。

# MyAIAgent V4 简单使用方法

## 1. 下载当前 V4

克隆仓库：

```bash
git clone https://github.com/wangdechun202308-commits/chatgpt2api.git
cd chatgpt2api
```

切换到当前 MyAIAgent V4 分支：

```bash
git checkout myaiagent-v4
```

确认当前分支：

```bash
git branch --show-current
```

应显示：

```text
myaiagent-v4
```

当前 V4 GitHub 基线为：

```text
9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

可执行：

```bash
git rev-parse HEAD
```

进行确认。

---

## 2. 按原 chatgpt2api 方法完成配置并启动

先按照原 `chatgpt2api` 项目的说明配置自己的运行环境和账号，然后启动 `chatgpt2api`。

确认容器已经运行：

```bash
docker ps
```

应当能够看到：

```text
chatgpt2api
```

---

## 3. 安装 MyAIAgent V4 Tool Bridge

确认当前位于 `chatgpt2api` 项目目录：

```bash
pwd
```

然后将 V4 协议文件复制进正在运行的容器：

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

如果没有报错，再重新启动容器：

```bash
docker restart chatgpt2api
```

确认容器重新运行：

```bash
docker ps
```

---

## 4. 检查是否安装了当前 V4

执行：

```bash
docker exec chatgpt2api \
  sha256sum \
  /app/services/protocol/openai_v1_chat_complete.py
```

当前经过生产验证的 MyAIAgent V4 文件 SHA256 为：

```text
69ab9b7edc8edafc334e998b5367a4fedb10c46b5188abeb58f805602f7f6eea
```

如果输出完全一致，即表示当前 V4 Tool Bridge 文件已经正确安装。

---

## 5. 当前版本说明

当前推荐版本：

```text
Branch: myaiagent-v4
Git HEAD: 9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

当前 V4 包含：

```text
MyAIAgent V4 JSON Tool Bridge
当前任务链 Tool Round 修复
Auto Tool Recovery
Exact Search Echo Recovery
Strict Tool Call Recovery
```

旧标签：

```text
myaiagent-v4-stable
```

仍然保留作为早期 V4 稳定基线，但它不包含后来完成的四项生产修复，因此不建议新安装继续使用该标签。

---

## 6. 关于上游版本

当前 `myaiagent-v4` 是独立维护的 V4 分支。

当前上游 `yukkcat/main` 已经迁移到另一套 Git 历史，与 `myaiagent-v4` 没有共同 Git ancestor，因此 GitHub Compare 页面可能显示：

```text
There isn’t anything to compare.
entirely different commit histories.
```

这不表示 V4 下载失败，也不表示 V4 没有修改。

安装 MyAIAgent V4 时，应直接使用：

```bash
git checkout myaiagent-v4
```

不要根据 GitHub 与当前 upstream/main 的 Compare 页面判断 V4 是否正确安装。
