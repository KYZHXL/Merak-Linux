#!/bin/bash
# ============================================
# 天璇 Merak-Linux 一键部署脚本
# 支持 Ubuntu/Debian + CentOS/Rocky/Alma
# 用法：sudo bash setup.sh
# ============================================
set -e

# 颜色
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/merak"

# 检测系统包管理器
detect_pkg() {
  if command -v apt-get >/dev/null 2>&1; then echo "apt"
  elif command -v dnf >/dev/null 2>&1; then echo "dnf"
  elif command -v yum >/dev/null 2>&1; then echo "yum"
  else echo "unknown"; fi
}
PKG=$(detect_pkg)

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    info "Docker 已安装: $(docker --version)"
    return
  fi
  info "安装 Docker..."
  case "$PKG" in
    apt)
      apt-get update -y
      apt-get install -y ca-certificates curl gnupg
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
      apt-get update -y
      apt-get install -y docker-ce docker-ce-cli containerd.io
      ;;
    dnf|yum)
      $PKG install -y yum-utils
      $PKG config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      $PKG install -y docker-ce docker-ce-cli containerd.io
      systemctl enable --now docker
      ;;
  esac
  info "Docker 安装完成"
}

install_python() {
  if command -v python3 >/dev/null 2>&1 && python3 --version | grep -qE "3\.[1-9][0-9]|3\.1[0-9]"; then
    info "Python3 已装: $(python3 --version)"
  else
    info "安装 Python3..."
    case "$PKG" in
      apt) apt-get install -y python3 python3-pip python3-venv ;;
      dnf|yum) $PKG install -y python3 python3-pip ;;
    esac
  fi
}

main() {
  [ "$EUID" -ne 0 ] && { err "请用 root 运行：sudo bash setup.sh"; exit 1; }
  [ "$PKG" = "unknown" ] && { err "不支持的系统"; exit 1; }

  info "检测到包管理器: $PKG"
  apt-get update -y 2>/dev/null || $PKG update -y

  info "安装依赖..."
  case "$PKG" in
    apt) apt-get install -y curl git ;;
    dnf|yum) $PKG install -y curl git ;;
  esac

  install_python
  install_docker

  # 复制项目到 /opt/merak
  info "部署项目到 $INSTALL_DIR ..."
  mkdir -p "$INSTALL_DIR"
  rsync -a --exclude='__pycache__' "$SCRIPT_DIR/" "$INSTALL_DIR/" 2>/dev/null || cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"
  cd "$INSTALL_DIR"

  # Python 虚拟环境 + 依赖
  info "安装 Python 依赖..."
  python3 -m venv venv
  ./venv/bin/pip install --upgrade pip -q
  ./venv/bin/pip install -r requirements.txt -q

  # 创建 merak 用户
  id merak >/dev/null 2>&1 || useradd -r -s /bin/false merak
  chown -R merak:merak "$INSTALL_DIR"

  # systemd 服务
  info "配置 systemd 服务..."
  cp "$INSTALL_DIR/deploy/merak.service" /etc/systemd/system/merak.service
  systemctl daemon-reload
  systemctl enable merak

  # 环境变量模板
  if [ ! -f "$INSTALL_DIR/deploy/merak-env.sh" ]; then
    cat > "$INSTALL_DIR/deploy/merak-env.sh" << 'EOF'
# 天璇 Merak 环境变量（用户部署时填写）
# 必填：AI 模型 API key（如 DeepSeek）
YOUNCHAT_API_KEY=你的API_Key
EOF
    chown merak:merak "$INSTALL_DIR/deploy/merak-env.sh"
  fi

  info "部署完成！"
  echo ""
  echo "接下来："
  echo "  1. 编辑环境变量:  nano $INSTALL_DIR/deploy/merak-env.sh   (填 YOUNCHAT_API_KEY)"
  echo "  2. 启动服务:      systemctl start merak"
  echo "  3. 查看 NapCat 登录二维码: docker logs -f napcat"
  echo "  4. 访问 WebUI:    http://服务器IP:5173"
  echo ""
  warn "NapCat 首次启动需在 WebUI(http://服务器IP:6099/webui) 配置反向 WS（Host 127.0.0.1, Port 6700）"
}

main "$@"
