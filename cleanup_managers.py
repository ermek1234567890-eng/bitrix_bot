"""
Удалить уволенных менеджеров из базы данных
Оставить только активных
"""
import asyncio
from dotenv import load_dotenv
from db import init_db, get_managers, toggle_manager
from bitrix import Bitrix

async def cleanup():
    init_db()

    bx = Bitrix()

    # Получаем всех активных менеджеров из Bitrix24
    print("Loading active managers from Bitrix24...")
    active_bitrix = await bx.get_users(position=None, positions=None)
    active_ids = set(int(u.get("ID")) for u in active_bitrix if u.get("ACTIVE") == "Y")
    print(f"Active in Bitrix24: {len(active_ids)} managers")
    print(f"IDs: {sorted(active_ids)}")

    # Получаем всех менеджеров из БД
    print("\nManagers in database:")
    db_managers = get_managers()

    deactivated = []
    for mgr in db_managers:
        bid = mgr["bitrix_id"]
        name = mgr["short_name"]
        is_active = mgr["is_active"]

        if bid not in active_ids:
            print(f"  ❌ {name} (ID:{bid}) - УВОЛЕН, удаляем из базы")
            toggle_manager(bid, 0)  # Деактивируем
            deactivated.append(name)
        else:
            status = "✅ Active" if is_active else "⚠️  Inactive"
            print(f"  {status}: {name} (ID:{bid})")

    print()
    if deactivated:
        print(f"Удалено {len(deactivated)} менеджеров: {', '.join(deactivated)}")
    else:
        print("Все менеджеры в базе активны в Bitrix24!")

if __name__ == "__main__":
    asyncio.run(cleanup())
