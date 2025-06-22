#!/usr/bin/env python3
import os
import sys
import argparse
import getpass
import time
import re
from datetime import datetime
from netmiko import ConnectHandler
from colorama import Fore, Style, init

init(autoreset=True)
LOG_FILE = "mikrotik_update.log"

def log(message: str):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {message}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)

def read_ip_list(file_path):
    if not os.path.isfile(file_path):
        print(Fore.RED + f"[!] Файл не найден: {file_path}")
        sys.exit(1)
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]

def gather_info(ip, username, password):
    log(f"{Fore.CYAN}Подключение к {ip}...")

    device = {
        "device_type": "mikrotik_routeros",
        "ip": ip,
        "username": username,
        "password": password,
        "timeout": 15,
        "read_timeout_override": 15
    }

    try:
        conn = ConnectHandler(**device)
    except Exception as e:
        log(f"{Fore.RED}[!] Ошибка подключения к {ip}: {e}")
        return

    try:
        current_version = ""
        for attempt in range(2):
            version_output = conn.send_command("/system package update print")
            time.sleep(2)
            for line in version_output.splitlines():
                match = re.search(r"installed-version\s*:\s*(\S+)", line)
                if match:
                    current_version = match.group(1)
                    break
            if current_version:
                break
            else:
                log(f"{Fore.YELLOW}[?] installed-version не получен, повтор...")

        time.sleep(1)
        check_output = conn.send_command("/system package update check-for-updates")
        conn.disconnect()

        latest_version = ""
        update_status = ""

        for line in check_output.splitlines():
            if "latest-version" in line:
                latest_version = line.split(":")[1].strip()
            if "status" in line:
                update_status = line.split(":")[1].strip()

        if current_version:
            msg = f"{ip} — Версия: {Fore.YELLOW}{current_version}"
            if update_status.lower().startswith("new"):
                msg += f" → {Fore.GREEN}Доступна новая: {latest_version}"
            else:
                msg += f" — {Fore.CYAN}Обновлений нет"
            log(msg)
        else:
            log(f"{Fore.RED}[!] Не удалось определить текущую версию на {ip}")

    except Exception as e:
        log(f"{Fore.RED}[!] Ошибка при выполнении команд на {ip}: {e}")

def reboot_and_wait(ip, username, password, wait_time=300):
    log(f"{ip} — Ожидание возврата после перезагрузки (до {wait_time} сек)...")
    time.sleep(30)
    for _ in range(int(wait_time / 15)):
        try:
            conn = ConnectHandler(
                device_type="mikrotik_routeros",
                ip=ip,
                username=username,
                password=password,
                timeout=10
            )
            conn.disconnect()
            log(f"{ip} — Устройство снова в сети")
            return
        except Exception:
            time.sleep(15)
    log(f"{Fore.RED}[!] {ip} — Не отвечает после таймаута {wait_time} сек")

def upgrade_device(ip, username, password, auto_reboot=True):
    log(f"{Fore.CYAN}Подключение к {ip} для обновления...")

    device = {
        "device_type": "mikrotik_routeros",
        "ip": ip,
        "username": username,
        "password": password,
        "timeout": 30,
        "read_timeout_override": 30
    }

    try:
        conn = ConnectHandler(**device)
    except Exception as e:
        log(f"{Fore.RED}[!] Ошибка подключения к {ip}: {e}")
        return

    try:
        check_output = conn.send_command("/system package update check-for-updates")
        latest_version = ""
        status = ""

        for line in check_output.splitlines():
            if "latest-version" in line:
                latest_version = line.split(":")[1].strip()
            if "status" in line:
                status = line.split(":")[1].strip().lower()

        if "new version is available" in status:
            log(f"{ip} — Доступно обновление до {latest_version}. Устанавливаю...")
            try:
                output = conn.send_command_timing("/system package update install")
                if "Do you want to upgrade" in output:
                    conn.write_channel("y\n")
                    time.sleep(5)
                    log(f"{ip} — Команда подтверждена. Ожидание перезагрузки...")
                else:
                    log(f"{ip} — Ответ: {output.strip()}")
                    if "up to date" in output.lower():
                        log(f"{ip} — Уже обновлено. Пропускаем перезагрузку.")
                        conn.disconnect()
                        return
                    else:
                        log(f"{ip} — Обновление возможно началось без подтверждения.")
                conn.disconnect()
                if auto_reboot:
                    reboot_and_wait(ip, username, password, wait_time=300)
            except Exception as e:
                log(f"{Fore.RED}[!] Ошибка при установке обновления на {ip}: {e}")
                conn.disconnect()
                return
        else:
            log(f"{ip} — Обновлений пакетов нет ({status}), проверка RouterBOARD...")

        # Проверка RouterBOARD даже если пакетов не было
        try:
            rb_conn = ConnectHandler(**device)
            rb_info = rb_conn.send_command("/system routerboard print")
            curr_fw, upg_fw = "", ""
            for line in rb_info.splitlines():
                if "current-firmware" in line:
                    curr_fw = line.split(":")[1].strip()
                if "upgrade-firmware" in line:
                    upg_fw = line.split(":")[1].strip()
            if curr_fw and upg_fw and curr_fw != upg_fw:
                log(f"{ip} — Требуется RouterBOARD upgrade: {curr_fw} → {upg_fw}")
                rb_conn.send_command("/system routerboard upgrade", expect_string=r"\[y/n\]")
                rb_conn.write_channel("y\n")
                time.sleep(3)
                rb_conn.disconnect()

                log(f"{ip} — RouterBOARD upgrade выполнен. Отправляю команду на перезагрузку...")
                try:
                    reboot_conn = ConnectHandler(**device)
                    output = reboot_conn.send_command_timing("/system reboot")
                    if "y/n" in output.lower():
                        reboot_conn.write_channel("y\n")
                        time.sleep(2)
                    reboot_conn.disconnect()
                except Exception:
                    pass  # Скорее всего ушёл в ребут

                reboot_and_wait(ip, username, password, wait_time=240)
            else:
                log(f"{ip} — RouterBOARD уже актуален ({curr_fw})")
            rb_conn.disconnect()
        except Exception as e:
            log(f"{Fore.RED}[!] Ошибка при проверке RouterBOARD: {e}")

    except Exception as e:
        log(f"{Fore.RED}[!] Ошибка при обновлении {ip}: {e}")

def main():
    parser = argparse.ArgumentParser(description="MikroTik RouterOS массовое обновление")
    parser.add_argument("--mode", choices=["gather", "upgrade"], required=True, help="Режим работы")
    parser.add_argument("--ip", help="IP адрес устройства")
    parser.add_argument("--file", help="Файл со списком IP адресов")
    parser.add_argument("--user", help="Имя пользователя MikroTik")
    args = parser.parse_args()

    if not args.ip and not args.file:
        print(Fore.RED + "[!] Укажите --ip или --file")
        sys.exit(1)

    username = args.user or input("Username: ")
    password = getpass.getpass("Password: ")

    targets = [args.ip] if args.ip else read_ip_list(args.file)

    if args.mode == "gather":
        for ip in targets:
            gather_info(ip, username, password)
            time.sleep(1)

    elif args.mode == "upgrade":
        for ip in targets:
            upgrade_device(ip, username, password, auto_reboot=True)
            time.sleep(1)

if __name__ == "__main__":
    main()
