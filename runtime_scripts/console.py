"""Interactive, local start/stop console for this deployment only."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(action):
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'manage.py'), action], cwd=ROOT)
        if result.returncode:
            print('\n操作未完成，请查看 logs 文件夹中的日志。')
        elif action == 'stop':
            print('\n机器人已关闭。微信登录状态、角色和记忆已保留。')
        elif action == 'start':
            print('\n机器人已启动。群里原生 @或输入“@大肥鱼”即可聊天。')
    except Exception as exc:
        print('\n操作失败：' + str(exc))


def main():
    parser = argparse.ArgumentParser(description='大肥鱼微信机器人控制台')
    parser.add_argument('--action', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    if args.action:
        run(args.action)
    while True:
        print('\n════════ 大肥鱼微信机器人 ════════')
        print('  1  启动机器人\n  2  关闭机器人\n  3  重启机器人\n  4  打开管理后台\n  5  查看运行状态\n  6  管理群聊（搜索群名后选择）\n  7  管理表情库（同步手机添加的表情包）\n  0  退出控制台')
        print('后台：http://127.0.0.1:6185\n关闭本窗口只退出控制台，机器人继续运行。需要停机请选 2。')
        try:
            choice = input('\n请输入数字并回车：').strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == '0':
            return
        if choice == '6':
            from group_manager import run_menu
            run_menu(ROOT)
        elif choice == '7':
            from sticker_manager import run_menu
            run_menu()
        elif choice == '3':
            if subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'stop'], cwd=ROOT).returncode == 0:
                run('start')
        elif choice in {'1','2','4','5'}:
            run({'1':'start','2':'stop','4':'open','5':'status'}[choice])
        else:
            print('请输入菜单中的数字。')


if __name__ == '__main__':
    main()
