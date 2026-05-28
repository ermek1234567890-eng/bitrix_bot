"""
Run both bots in parallel using multiprocessing.
- bot.py      (Report bot)       — TELEGRAM_TOKEN
- mop_bot.py  (MOP Reminder bot) — MOP_BOT_TOKEN
"""
import multiprocessing
import sys


def run_report_bot():
    from bot import main
    main()


def run_mop_bot():
    from mop_bot import main
    main()


if __name__ == "__main__":
    procs = [
        multiprocessing.Process(target=run_report_bot, name="report_bot"),
        multiprocessing.Process(target=run_mop_bot, name="mop_bot"),
    ]
    for p in procs:
        p.start()
        print(f"Started {p.name} (pid={p.pid})")

    # Wait for both — if one dies, stop the other
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        print("Shutting down...")
        for p in procs:
            p.terminate()
        sys.exit(0)
