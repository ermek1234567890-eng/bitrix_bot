"""Runs once on first start to seed settings, managers and projects into DB."""
import sqlite3
from db import init_db, db_get, db_set, upsert_manager, upsert_project

SETTINGS = {
    "meeting_date_field": "UF_CRM_1752403539",
    "booking_date_field": "UF_CRM_1754145255",
    "paid_date_field":    "UF_CRM_1754145323",
    "sales_pipeline_id":  "2",
    "cs_pipeline_id":     "6",
    "agent_stage_ids":    "C2:UC_8UPW0W",
    "ped_stage_ids":      "C2:PREPARATION",
    "closed_stage_ids":   "C2:LOSE,C2:UC_ZPEQN9",
    "source_field":       "UF_CRM_1751630224",
    "project_field":      "UF_CRM_1758630528",
    "source_tm_ids":      "1514,1660",
    "source_agent_ids":   "2008",
    "source_ped_ids":     "1516",
    "op_manager_field":   "UF_CRM_1751600120",
}

MANAGERS = [
    (27396,  "Мухамеджан Нургуль",   "Нургуль"),
    (45844,  "Ниязов Сабир",         "Сабир"),
    (46284,  "Мырзакулов Асхат",     "Асхат"),
    (49464,  "Абдрузак Заир",        "Заир"),
    (62528,  "Худайкулова Гюзаль",   "Гюзаль"),
    (66566,  "Ким Яна",              "Яна"),
    (67332,  "Музапбаров Олжас",     "Олжас"),
    (67442,  "Алиядинова Самал",     "Самал"),
    (68542,  "Қайса Ернур",          "Ернур"),
    (68544,  "Жумадилов Асет",       "Асет"),
    (68800,  "Бекназар Аль-Нур",     "Аль-Нур"),
    (68840,  "Отжағарова Жазира",    "Жазира"),
    (68894,  "Нурова Салима",        "Салима"),
]

PROJECTS = [
    ("Атмосфера",   "1962"),
    ("Keruen",      "1964"),
    ("Aqsai Resort","1966"),
    ("Aura",        "1968"),
    ("Arlan",       "3676"),
    ("Bravo",       "3346"),
    ("Discovery",   "3348"),
    ("Atakent",     "3350"),
    ("Neo",         "3352"),
]


def seed():
    init_db()
    for key, val in SETTINGS.items():
        if not db_get(key):
            db_set(key, val)
    for bid, name, short in MANAGERS:
        upsert_manager(bid, name, short)
    for name, eid in PROJECTS:
        upsert_project(name, eid)


if __name__ == "__main__":
    seed()
    print("Seed complete")
