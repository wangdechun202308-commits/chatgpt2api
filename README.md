本版本在原项目基础上进行了个人测试用途的改进，将原有的文本模拟工具调用调整为可由本地 Agent 执行真实工具调用。

本项目仅供个人学习、研究和测试使用。

使用者应遵守 OpenAI 及相关服务提供方的使用规则，不得将本项目用于违法、违规、滥用或恶意用途。

# MyAIAgent V4 简单使用方法

## 1. 下载 MyAIAgent V4

克隆仓库：

```bash
git clone https://github.com/wangdechun202308-commits/chatgpt2api.git
cd chatgpt2api
```

切换到 MyAIAgent V4 分支：

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

当前经过验证的 V4 Git 基线：

```text
9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

可以执行：

```bash
git rev-parse HEAD
```

进行确认。

如果需要精确使用当前已经验证过的版本，也可以直接检出该提交：

```bash
git checkout 9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

---

## 2. 按原 chatgpt2api 方法完成配置并启动

先按照原 `chatgpt2api` 项目的说明完成运行环境、账号及相关配置，然后启动 `chatgpt2api`。

确认容器已经运行：

```bash
docker ps
```

正常情况下应能够看到：

```text
chatgpt2api
```

如果你的容器名称不是 `chatgpt2api`，请在下面的命令中将 `chatgpt2api` 替换为实际容器名称。

---

## 3. 安装 MyAIAgent V4 Tool Bridge

确认当前位于刚刚克隆的 `chatgpt2api` 项目目录：

```bash
pwd
```

将 V4 协议文件复制到正在运行的 `chatgpt2api` 容器：

```bash
docker cp \
  services/protocol/openai_v1_chat_complete.py \
  chatgpt2api:/app/services/protocol/openai_v1_chat_complete.py
```

复制完成后检查 Python 语法：

```bash
docker exec chatgpt2api \
  /app/.venv/bin/python \
  -m py_compile \
  /app/services/protocol/openai_v1_chat_complete.py
```

如果命令没有报错，再重新启动容器：

```bash
docker restart chatgpt2api
```

确认容器已经重新运行：

```bash
docker ps
```

---

## 4. 验证当前 V4 是否安装正确

执行：

```bash
docker exec chatgpt2api \
  sha256sum \
  /app/services/protocol/openai_v1_chat_complete.py
```

当前经过实际生产环境验证的 MyAIAgent V4 协议文件 SHA256 为：

```text
69ab9b7edc8edafc334e998b5367a4fedb10c46b5188abeb58f805602f7f6eea
```

如果输出的 SHA256 与上面完全一致，说明当前 V4 Tool Bridge 文件已经正确安装。

---

## 5. 当前版本

当前推荐版本：

```text
Branch: myaiagent-v4
Git HEAD: 9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

当前 V4 包含的主要改进：

```text
MyAIAgent V4 JSON Tool Bridge
当前任务链 Tool Round 修复
Auto Tool Recovery
Exact Search Echo Recovery
Strict Tool Call Recovery
```

主要目标是提高模型在本地 Agent 环境中的结构化 Tool Call、多轮工具调用以及异常输出恢复的可靠性。

---

## 6. 关于旧版 stable 标签

仓库中仍然保留旧标签：

```text
myaiagent-v4-stable
```

该标签属于早期 V4 稳定基线。

它不包含后来加入的四项 Tool Call 相关修复，因此：

```text
新安装：建议使用 myaiagent-v4
历史回滚：可保留 myaiagent-v4-stable
```

不建议新安装继续使用旧的 `myaiagent-v4-stable` 标签。

---

## 7. 关于上游版本

当前 `myaiagent-v4` 是独立维护的 V4 分支。

当前上游 `yukkcat/main` 已经使用另一套 Git 历史，与本分支不存在共同 Git ancestor。

因此 GitHub Compare 页面可能显示：

```text
There isn’t anything to compare.
yukkcat:main and wangdechun202308-commits:myaiagent-v4
are entirely different commit histories.
```

并可能显示：

```text
0 additions
0 deletions
```

这并不表示：

```text
V4 没有上传
V4 没有修改
V4 下载失败
```

而只是因为 GitHub 无法对两套没有共同祖先的 Git 历史进行普通的分支差异比较。

安装 MyAIAgent V4 时，请直接使用：

```bash
git checkout myaiagent-v4
```

并通过 Git commit 和容器内协议文件 SHA256 判断版本是否正确。

---

## 8. 最简单的版本确认方法

Git 版本：

```bash
git rev-parse HEAD
```

当前验证基线：

```text
9b2d77c2abc073c0b578609bef4b414f8f7c67b6
```

容器内 V4 协议文件：

```bash
docker exec chatgpt2api \
  sha256sum \
  /app/services/protocol/openai_v1_chat_complete.py
```

当前验证 SHA256：

```text
69ab9b7edc8edafc334e998b5367a4fedb10c46b5188abeb58f805602f7f6eea
```

这两个版本信息都符合，即可确认当前安装的是本次已经验证过的 MyAIAgent V4。
