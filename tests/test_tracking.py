"""
tests/test_tracking.py — Тесты сбора метрик из ShortsProject и PreLend.

Все тесты используют временные файлы (conftest.py fixtures).
Реальные репозитории не читаются.

Покрывает:
    - collect_shorts_project_snapshot: чтение analytics.json
    - collect_prelend_snapshot: чтение clicks.db + conversions
    - Корректность расчёта CTR, CR, bot_pct
    - Поведение при отсутствии файлов (graceful degradation)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta


class TestShortsProjectTracking:
    """collect_shorts_project_snapshot: чтение и агрегация analytics.json."""

    def test_empty_analytics_returns_zeros(self, init_database):
        """Если analytics.json отсутствует → метрики 0, без исключений."""
        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()
        assert result["total_views"] == 0
        assert result["total_likes"] == 0
        assert result["avg_ctr"] is None
        assert result["ban_count"] == 0

    def test_reads_views_and_likes(self, make_sp_analytics):
        """Суммирует views/likes из analytics.json."""
        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "title": "Test Video 1",
                "tags": ["test"],
                "uploads": {
                    "youtube": {
                        "url": "https://yt.be/1",
                        "uploaded_at": now_iso,
                        "views": 1000,
                        "likes": 50,
                        "comments": 10,
                        "ab_variant": "A",
                    }
                }
            },
            "vid_002": {
                "title": "Test Video 2",
                "tags": [],
                "uploads": {
                    "tiktok": {
                        "url": "https://tiktok.com/2",
                        "uploaded_at": now_iso,
                        "views": 500,
                        "likes": 20,
                        "comments": 5,
                        "ab_variant": None,
                    }
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()

        assert result["total_views"] == 1500
        assert result["total_likes"] == 70

    def test_top_platform_detection(self, make_sp_analytics):
        """Определяет платформу с наибольшим числом просмотров."""
        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "uploads": {
                    "youtube":   {"uploaded_at": now_iso, "views": 100, "likes": 5,  "comments": 1},
                    "tiktok":    {"uploaded_at": now_iso, "views": 5000,"likes": 200,"comments": 50},
                    "instagram": {"uploaded_at": now_iso, "views": 200, "likes": 10, "comments": 2},
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()
        assert result["top_platform"] == "tiktok"

    def test_ctr_calculation(self, make_sp_analytics):
        """CTR = (likes + comments) / views."""
        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "uploads": {
                    "youtube": {
                        "uploaded_at": now_iso,
                        "views": 1000, "likes": 80, "comments": 20
                    }
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()

        assert result["avg_ctr"] is not None
        assert abs(result["avg_ctr"] - 0.10) < 0.001   # (80+20)/1000 = 0.10

    def test_filters_old_uploads(self, make_sp_analytics):
        """Загрузки старше period_hours не включаются в снапшот."""
        old_iso = (datetime.now() - timedelta(hours=48)).isoformat()
        make_sp_analytics({
            "old_vid": {
                "uploads": {
                    "youtube": {
                        "uploaded_at": old_iso,
                        "views": 9999, "likes": 999, "comments": 99
                    }
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot(period_hours=24)
        assert result["total_views"] == 0, "Старые загрузки не должны попадать в снапшот"

    def test_ab_summary_collected(self, make_sp_analytics):
        """A/B тесты обнаруживаются и попадают в ab_summary."""
        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_ab": {
                "uploads": {
                    "youtube": {"uploaded_at": now_iso, "views": 100, "ab_variant": "A"},
                    "tiktok":  {"uploaded_at": now_iso, "views": 200, "ab_variant": "B"},
                },
                "ab_test": {
                    "A": {"title": "Title A", "tags": []},
                    "B": {"title": "Title B", "tags": []},
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()
        assert len(result["ab_summary"]) == 1
        assert "A" in result["ab_summary"][0]["variants"]
        assert "B" in result["ab_summary"][0]["variants"]

    def test_reads_agent_statuses(self, make_sp_analytics, make_sp_memory):
        """Читает статусы агентов из agent_memory.json."""
        make_sp_analytics({})
        make_sp_memory(agents={"SCOUT": "RUNNING", "GUARDIAN": "IDLE"})

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()
        assert result["agent_statuses"].get("SCOUT") == "RUNNING"
        assert result["agent_statuses"].get("GUARDIAN") == "IDLE"


class TestPreLendTracking:
    """collect_prelend_snapshot: чтение clicks.db (реальная схема PreLend)."""

    def test_empty_db_returns_zeros(self, make_prelend_db):
        """Пустая БД → метрики 0, без исключений."""
        make_prelend_db(clicks=[], conversions=[])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 0
        assert result["conversions"] == 0
        assert result["cr"] is None

    def test_no_db_graceful(self):
        """Если clicks.db не существует → метрики 0, без исключений."""
        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 0

    def test_counts_clicks(self, make_prelend_db):
        """Считает клики из таблицы clicks."""
        now_ts = int(time.time())
        make_prelend_db(clicks=[
            {"click_id": f"c{i}", "ts": now_ts, "status": "sent"}
            for i in range(10)
        ])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 10

    def test_counts_conversions_from_conversions_table(self, make_prelend_db):
        """Конверсии берутся из таблицы conversions, не из clicks.status."""
        now_ts = int(time.time())
        make_prelend_db(
            clicks=[
                {"click_id": f"c{i}", "ts": now_ts, "status": "sent"}
                for i in range(20)
            ],
            conversions=[
                {"conv_id": f"v{i}", "date": "2024-01-01",
                 "advertiser_id": "adv1", "count": 1, "created_at": now_ts}
                for i in range(5)
            ]
        )

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["conversions"] == 5
        assert abs(result["cr"] - 5/20) < 0.0001

    def test_bot_pct_calculation(self, make_prelend_db):
        """bot_pct = bot_clicks / total_clicks * 100."""
        now_ts = int(time.time())
        make_prelend_db(clicks=[
            {"click_id": "c1", "ts": now_ts, "status": "sent"},
            {"click_id": "c2", "ts": now_ts, "status": "sent"},
            {"click_id": "c3", "ts": now_ts, "status": "bot"},
            {"click_id": "c4", "ts": now_ts, "status": "bot"},
        ])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 4
        assert abs(result["bot_pct"] - 50.0) < 0.1

    def test_cloaked_not_counted_as_bot(self, make_prelend_db):
        """'cloaked' (off-geo) — это не боты, отдельная категория."""
        now_ts = int(time.time())
        make_prelend_db(clicks=[
            {"click_id": "c1", "ts": now_ts, "status": "sent"},
            {"click_id": "c2", "ts": now_ts, "status": "cloaked"},  # off-geo, не бот
            {"click_id": "c3", "ts": now_ts, "status": "bot"},
        ])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        # Только 1 из 3 — бот (33.3%), не 2 из 3
        assert abs(result["bot_pct"] - 100/3) < 0.5

    def test_top_geo_detection(self, make_prelend_db):
        """Определяет самое популярное ГЕО по не-bot/cloaked кликам."""
        now_ts = int(time.time())
        make_prelend_db(clicks=[
            {"click_id": "c1", "ts": now_ts, "geo": "BR", "status": "sent"},
            {"click_id": "c2", "ts": now_ts, "geo": "BR", "status": "sent"},
            {"click_id": "c3", "ts": now_ts, "geo": "US", "status": "sent"},
        ])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["top_geo"] == "BR"

    def test_excludes_test_clicks(self, make_prelend_db):
        """Тестовые клики (is_test=1) не считаются."""
        now_ts = int(time.time())
        make_prelend_db(clicks=[
            {"click_id": "c1", "ts": now_ts, "status": "sent", "is_test": 0},
            {"click_id": "c2", "ts": now_ts, "status": "sent", "is_test": 1},  # тест
            {"click_id": "c3", "ts": now_ts, "status": "sent", "is_test": 1},  # тест
        ])

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 1


class TestCollectAllAndSave:
    """collect_all_and_save: интеграционный тест сохранения снапшотов в БД."""

    def test_saves_both_snapshots(self, init_database, make_sp_analytics, make_prelend_db):
        """Оба снапшота сохраняются в metrics_snapshots."""
        from db.metrics import get_latest_snapshot

        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "uploads": {"youtube": {"uploaded_at": now_iso, "views": 100, "likes": 5}}
            }
        })
        make_prelend_db(clicks=[
            {"click_id": "c1", "ts": int(time.time()), "status": "sent"}
        ])

        from modules.tracking import collect_all_and_save
        collect_all_and_save()

        sp_snap = get_latest_snapshot("ShortsProject")
        pl_snap = get_latest_snapshot("PreLend")

        assert sp_snap is not None
        assert pl_snap is not None
        assert sp_snap["sp_total_views"] == 100
        assert pl_snap["pl_total_clicks"] == 1
