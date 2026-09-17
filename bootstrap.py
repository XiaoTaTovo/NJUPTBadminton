"""Compatibility entry point. Routine startup never installs dependencies."""
import sys

def main():
    from launcher import main as launch
    return launch()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已退出。')
    except Exception as exc:
        print('启动失败：'+type(exc).__name__+'；环境修复请单独运行 setup_environment.bat。')
        sys.exit(2)
