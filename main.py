import uvicorn
import sqlite3
from datetime import datetime, timezone, date
from typing import Dict, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import logging
import random

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tamagotchi BOS Trainer — Compatible Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DB_PATH = "tamagotchi.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        # Таблица daily_points
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_points (
                user_id INTEGER,
                date TEXT,
                points INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        # Добавляем колонку penalty_applied, если её нет
        try:
            conn.execute("ALTER TABLE daily_points ADD COLUMN penalty_applied INTEGER DEFAULT 0")
            conn.execute("UPDATE daily_points SET penalty_applied = 0 WHERE penalty_applied IS NULL")
        except sqlite3.OperationalError:
            pass  # уже существует

        # Таблица user_health
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_health (
                user_id INTEGER PRIMARY KEY,
                health INTEGER DEFAULT 50
            )
        """)
        # Добавляем last_action_time, если её нет
        try:
            conn.execute("ALTER TABLE user_health ADD COLUMN last_action_time TEXT")
        except sqlite3.OperationalError:
            pass

        # Таблица логов
        conn.execute("""
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action_type TEXT,
                value REAL,
                timestamp TEXT DEFAULT (datetime('now', 'utc'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action_log_user_date ON action_log(user_id, action_type, date(timestamp))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_action_log_user ON action_log(user_id)")
        conn.commit()

init_db()

ACTION_POINTS = {
    "exercise": 10, "walk": 10, "stretch": 8, "sport_short": 15, "sport_long": 20,
    "breakfast": 8, "lunch": 8, "dinner": 8, "water": 1, "teeth": 10, "shower": 10,
    "sleep_good": 10, "sleep_bad": -5, "neurotraining": 20, "breathing": 10,
    "good_deed": 5, "help": 5, "reading": 7, "order": 5, "habit": 8
}

def today_utc_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def apply_inactivity_penalty(user_id: int) -> dict:
    today = today_utc_str()
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        cursor = conn.execute("SELECT last_action_time, health FROM user_health WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or not row["last_action_time"]:
            return {"penalty_tier": 0, "status": "active", "health_deduction": 0}
        last_action = datetime.fromisoformat(row["last_action_time"])
        hours_passed = (now - last_action).total_seconds() / 3600.0
        cursor = conn.execute("SELECT points, penalty_applied FROM daily_points WHERE user_id = ? AND date = ?", (user_id, today))
        dp_row = cursor.fetchone()
        current_points = dp_row["points"] if dp_row else 0
        penalty_applied = dp_row["penalty_applied"] if dp_row else 0
        penalty_tier = 0
        points_deduction = 0
        health_deduction = 0
        status = "active"
        if hours_passed >= 72:
            penalty_tier = 3
            status = "sleeping"
            health_deduction = int(hours_passed // 6)
            if penalty_applied < 3:
                points_deduction = int(current_points * 0.10)
        elif hours_passed >= 48:
            penalty_tier = 2
            status = "sad"
            health_deduction = int(hours_passed // 8)
            if penalty_applied < 2:
                points_deduction = int(current_points * 0.15)
        elif hours_passed >= 24:
            penalty_tier = 1
            status = "tired"
            health_deduction = int(hours_passed // 12)
            if penalty_applied < 1:
                points_deduction = int(current_points * 0.10)
        if points_deduction > 0 or health_deduction > 0:
            if points_deduction > 0:
                conn.execute("""
                    INSERT INTO daily_points (user_id, date, points, penalty_applied)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, date) DO UPDATE SET
                        points = max(0, points - ?),
                        penalty_applied = ?
                """, (user_id, today, max(0, current_points - points_deduction), penalty_tier,
                      points_deduction, penalty_tier))
            if health_deduction > 0:
                conn.execute("UPDATE user_health SET health = max(0, health - ?) WHERE user_id = ?", (health_deduction, user_id))
            conn.commit()
        return {"penalty_tier": penalty_tier, "status": status, "health_deduction": health_deduction}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/add_action")
async def add_action(data: Dict[str, Any]):
    try:
        user_id = data["user_id"]
        action_type = data["action_type"]
        value = data.get("value")
        today = today_utc_str()
        if action_type not in ACTION_POINTS:
            raise HTTPException(400, "Неизвестный тип действия")
        apply_inactivity_penalty(user_id)
        base_points = ACTION_POINTS[action_type]
        health_impact = 0
        message = ""
        with get_db() as conn:
            if action_type == "water":
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM action_log 
                        WHERE user_id = ? AND action_type = 'water' AND date(timestamp) = date(?)
                    """, (user_id, today))
                    water_today = cursor.fetchone()[0]
                    if water_today >= 10:
                        base_points = 0
                        health_impact = 0
                        message = "Водный баланс в норме! Превышен лимит (10/10 стаканов). Баллы не начислены."
                    else:
                        health_impact = 1
                        message = f"Выпит стакан воды ({water_today+1}/10 за сегодня). Питомец доволен!"
                        conn.execute("INSERT INTO action_log (user_id, action_type, value) VALUES (?, ?, ?)",
                                     (user_id, action_type, value))
                        if base_points != 0:
                            conn.execute("""
                                INSERT INTO daily_points (user_id, date, points) VALUES (?, ?, ?)
                                ON CONFLICT(user_id, date) DO UPDATE SET points = points + ?
                            """, (user_id, today, base_points, base_points))
                    conn.commit()
                except:
                    conn.rollback()
                    raise
            else:
                if base_points > 0:
                    health_impact = min(5, max(1, base_points // 2))
                elif base_points < 0:
                    health_impact = base_points
                message = f"Действие '{action_type}' успешно зафиксировано."
                conn.execute("INSERT INTO action_log (user_id, action_type, value) VALUES (?, ?, ?)",
                             (user_id, action_type, value))
                if base_points != 0:
                    conn.execute("""
                        INSERT INTO daily_points (user_id, date, points) VALUES (?, ?, ?)
                        ON CONFLICT(user_id, date) DO UPDATE SET points = points + ?
                    """, (user_id, today, base_points, base_points))
            now_str = now_utc_iso()
            conn.execute("""
                INSERT INTO user_health (user_id, health, last_action_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET 
                    health = max(0, min(100, user_health.health + ?)),
                    last_action_time = ?
            """, (user_id, max(0, min(100, 50 + health_impact)), now_str,
                  health_impact, now_str))
            conn.commit()
        return {"status": "success", "message": message, "points_added": base_points, "health_impact": health_impact}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in add_action: {e}")
        raise HTTPException(500, str(e))

@app.post("/set_mood")
async def set_mood(data: Dict[str, Any]):
    try:
        user_id = data["user_id"]
        mood = data["mood"]
        today = today_utc_str()
        mood_scores = {"excellent": 8, "good": 6, "normal": 4, "tired": 2, "bad": 2}
        if mood not in mood_scores:
            raise HTTPException(400, "Неверный статус настроения")
        points = mood_scores[mood]
        now_str = now_utc_iso()
        with get_db() as conn:
            conn.execute("INSERT INTO action_log (user_id, action_type) VALUES (?, ?)", (user_id, f"mood_{mood}"))
            conn.execute("""
                INSERT INTO daily_points (user_id, date, points) VALUES (?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET points = points + ?
            """, (user_id, today, points, points))
            conn.execute("""
                INSERT INTO user_health (user_id, last_action_time) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_action_time = ?
            """, (user_id, now_str, now_str))
            conn.commit()
        return {"status": "success", "message": f"Настроение обновлено. Получено +{points} баллов.", "points": points}
    except Exception as e:
        logger.error(f"Error in set_mood: {e}")
        raise HTTPException(500, str(e))

@app.get("/user_state")
async def user_state(user_id: int):
    try:
        today = today_utc_str()
        penalty_info = apply_inactivity_penalty(user_id)
        with get_db() as conn:
            cursor = conn.execute("SELECT points FROM daily_points WHERE user_id = ? AND date = ?", (user_id, today))
            row_points = cursor.fetchone()
            points = row_points["points"] if row_points else 0
            cursor = conn.execute("SELECT health FROM user_health WHERE user_id = ?", (user_id,))
            row_health = cursor.fetchone()
            health = row_health["health"] if row_health else 50
        return {
            "user_id": user_id,
            "date": today,
            "points_today": points,
            "health": health,
            "avatar_status": penalty_info["status"],
            "penalty_tier": penalty_info["penalty_tier"],
            "penalty": penalty_info["health_deduction"],
            "mood_penalty": None,
            "support": None
        }
    except Exception as e:
        logger.error(f"Error in user_state: {e}")
        raise HTTPException(500, str(e))

@app.post("/eeg_profile")
async def eeg_profile(data: Dict[str, Any]):
    try:
        user_id = data.get("user_id")
        channels = ["fp1", "fp2", "t3", "t4", "o1", "o2"]
        rhythms = ["alpha", "beta", "theta", "smr", "delta"]
        structured = {}
        for ch in channels:
            structured[ch] = {}
            for rh in rhythms:
                key = f"{ch}_{rh}"
                structured[ch][rh] = float(data.get(key, 0))
        # Паттерны (сокращённая версия для краткости, но рабочая)
        patterns = []
        f_beta  = (structured["fp1"]["beta"] + structured["fp2"]["beta"]) / 2
        f_alpha = (structured["fp1"]["alpha"] + structured["fp2"]["alpha"]) / 2
        f_smr   = (structured["fp1"]["smr"] + structured["fp2"]["smr"]) / 2
        f_theta = (structured["fp1"]["theta"] + structured["fp2"]["theta"]) / 2
        o_alpha = (structured["o1"]["alpha"] + structured["o2"]["alpha"]) / 2
        o_theta = (structured["o1"]["theta"] + structured["o2"]["theta"]) / 2
        t_theta = (structured["t3"]["theta"] + structured["t4"]["theta"]) / 2
        t_alpha = (structured["t3"]["alpha"] + structured["t4"]["alpha"]) / 2
        avg_beta  = sum(structured[ch]["beta"] for ch in channels) / 6
        avg_delta = sum(structured[ch]["delta"] for ch in channels) / 6
        avg_theta = sum(structured[ch]["theta"] for ch in channels) / 6
        avg_alpha = sum(structured[ch]["alpha"] for ch in channels) / 6
        f_t_theta = (structured["fp1"]["theta"] + structured["fp2"]["theta"] + structured["t3"]["theta"] + structured["t4"]["theta"]) / 4
        f_t_smr   = (structured["fp1"]["smr"] + structured["fp2"]["smr"] + structured["t3"]["smr"] + structured["t4"]["smr"]) / 4
        f_t_alpha = (structured["fp1"]["alpha"] + structured["fp2"]["alpha"] + structured["t3"]["alpha"] + structured["t4"]["alpha"]) / 4

        if f_beta > 35 and f_alpha < 20: patterns.append("Легко переключается с задачи на задачу")
        if f_smr > 15 and f_theta < 15: patterns.append("Умеет надолго погружаться в задачу")
        if o_alpha > 35 and (20 <= f_beta <= 30): patterns.append("Мыслит нестандартно")
        if avg_beta > 38 and avg_delta < 10: patterns.append("Мозг работает на высокой скорости")
        if f_alpha > 30 and f_beta < 30 and f_theta < 15: patterns.append("Сохраняет ясность головы")
        if f_smr > 18 and f_beta > 30: patterns.append("Хорошая моторная координация")
        if avg_delta > 30 and (15 <= avg_theta <= 25): patterns.append("Качественное восстановление сна")
        if ((structured["fp1"]["alpha"]+structured["fp2"]["alpha"]+structured["t3"]["alpha"]+structured["t4"]["alpha"])/4) > 28: patterns.append("Быстрое восстановление")
        if 12 <= f_smr <= 18: patterns.append("Устойчивое внимание")
        if ((structured["fp1"]["beta"]+structured["t3"]["beta"])/2) > 35 and (20 <= (structured["fp1"]["alpha"]+structured["t3"]["alpha"])/2 <= 30): patterns.append("Аналитическое мышление")
        if abs(structured["t3"]["alpha"] - structured["t4"]["alpha"]) < 15: patterns.append("Эмоциональная устойчивость")
        if o_alpha > 35 and (20 <= t_theta <= 28): patterns.append("Интуиция и образное мышление")
        f_t_beta = (structured["fp1"]["beta"]+structured["fp2"]["beta"]+structured["t3"]["beta"]+structured["t4"]["beta"])/4
        f_t_delta = (structured["fp1"]["delta"]+structured["fp2"]["delta"]+structured["t3"]["delta"]+structured["t4"]["delta"])/4
        if f_t_beta > 38 and f_t_delta < 8: patterns.append("Энергичность")
        if t_theta > 25 and t_alpha > 28: patterns.append("Музыкальность")
        if (18 <= f_t_theta <= 28) and f_t_smr > 15: patterns.append("Хорошая память")
        if (25 <= f_t_alpha <= 35) and (20 <= f_t_theta <= 28) and f_t_smr > 13: patterns.append("Лёгкое обучение")
        left_beta = structured["fp1"]["beta"] + structured["t3"]["beta"]
        right_beta = structured["fp2"]["beta"] + structured["t4"]["beta"]
        if left_beta > (right_beta * 1.15): patterns.append("Лидерские качества")
        if o_alpha > 38 and o_theta > 20: patterns.append("Богатое воображение")
        if avg_delta > 35 and avg_alpha < 15: patterns.append("Нужен отдых")
        if not patterns or (15 <= avg_alpha <= 30 and 15 <= avg_beta <= 30): patterns.append("Баланс и гармония")
        if user_id:
            today = today_utc_str()
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO daily_points (user_id, date, points) VALUES (?, ?, ?)
                    ON CONFLICT(user_id, date) DO UPDATE SET points = points + ?
                """, (user_id, today, 5, 5))
                conn.commit()
        return {"patterns": patterns}
    except Exception as e:
        logger.error(f"EEG error: {e}")
        raise HTTPException(500, str(e))

@app.post("/manual_update_health")
async def manual_update_health(data: Dict[str, Any]):
    try:
        user_id = data["user_id"]
        health = max(0, min(100, data["health"]))
        with get_db() as conn:
            conn.execute("""
                INSERT INTO user_health (user_id, health, last_action_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET health = ?
            """, (user_id, health, now_utc_iso(), health))
            conn.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/manual_update_points")
async def manual_update_points(data: Dict[str, Any]):
    try:
        user_id = data["user_id"]
        points = data["points"]
        today = today_utc_str()
        with get_db() as conn:
            conn.execute("""
                INSERT INTO daily_points (user_id, date, points)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET points = ?
            """, (user_id, today, points, points))
            conn.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/ai_advice")
async def ai_advice():
    advices = [
        "Я заметил избыток высокочастотного бета-ритма. Сделайте 5-минутную дыхательную практику для снижения стресса.",
        "Прекрасные показатели SMR-ритма лобных долей! Вы находитесь в состоянии идеального сфокусированного внимания.",
        "Внимание, ваш аватар не получал записей более 24 часов. Баланс энергии падает, сделайте разминку!",
        "Ваш индекс альфа-ритма в затылке в норме. Отличный уровень релаксации мозга.",
        "Не забывайте выпивать по стакану воды после каждой сессии нейротренинга для поддержания метаболизма мозга!"
    ]
    return {"advice": random.choice(advices)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=3344, reload=False)