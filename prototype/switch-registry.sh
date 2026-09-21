#!/usr/bin/env bash
# 在「公开源」和「百度内网源」之间切换 prototype 的 npm 配置。
#
# 用法：./switch-registry.sh public    # 公开源（registry.npmjs.org）
#       ./switch-registry.sh baidu     # 百度内网源（registry.npm.baidu-int.com）
#
# 两个环境各有一份锁文件，因为锁文件里的 resolved 地址写死了源；切换时会一起换掉
# .npmrc 和 package-lock.json（这两份是本地文件，不入库；.public/.baidu 变体入库）。
# 切换后照常 `npm ci` 即可。

set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
env="${1:-}"

case "$env" in
  public | baidu) ;;
  *)
    echo "用法：$0 public|baidu" >&2
    exit 2
    ;;
esac

for f in ".npmrc.$env" "package-lock.$env.json"; do
  [ -f "$dir/$f" ] || { echo "缺少 $f" >&2; exit 1; }
done

cp "$dir/.npmrc.$env" "$dir/.npmrc"
cp "$dir/package-lock.$env.json" "$dir/package-lock.json"

echo "已切到 $env"
echo "  registry -> $(sed -n 's/^registry=//p' "$dir/.npmrc")"
echo "  锁文件   -> package-lock.$env.json"
echo "接着跑：npm ci"
