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
                    "vk": {
                        "url": "https://vk.com/video1",
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
                    "rutube": {
                        "url": "https://rutube.ru/video/2",
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
                    "vk":     {"uploaded_at": now_iso, "views": 100, "likes": 5,  "comments": 1},
                    "rutube": {"uploaded_at": now_iso, "views": 300,"likes": 20,"comments": 5},
                    "ok":     {"uploaded_at": now_iso, "views": 5000, "likes": 200, "comments": 50},
                }
            }
        })

        from modules.tracking import collect_shorts_project_snapshot
        result = collect_shorts_project_snapshot()
        assert result["top_platform"] == "ok"

    def test_ctr_calculation(self, make_sp_analytics):
        """CTR = (likes + comments) / views."""
        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "uploads": {
                    "vk": {
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
                    "vk": {
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
                    "vk":     {"uploaded_at": now_iso, "views": 100, "ab_variant": "A"},
                    "rutube": {"uploaded_at": now_iso, "views": 200, "ab_variant": "B"},
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
    """
    collect_prelend_snapshot: данные получаются через PreLend Internal API (HTTP).
    Прямой доступ к clicks.db убран — тесты используют mock_prelend_client (conftest.py).
    """

    def test_api_unavailable_returns_zeros(self, mock_prelend_client):
        """API недоступен → метрики 0, _unreachable=True, без исключений."""
        mock_prelend_client.is_available.return_value = False

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 0
        assert result["conversions"] == 0
        assert result["cr"] is None
        assert result["_unreachable"] is True

    def test_api_unavailable_graceful(self, mock_prelend_client):
        """API недоступен → возвращается пустой снапшот без исключений."""
        mock_prelend_client.is_available.return_value = False

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 0
        assert result["bot_pct"] is None
        assert result["top_geo"] is None

    def test_api_returns_clicks(self, mock_prelend_client):
        """API возвращает клики → total_clicks корректно отражается в снапшоте."""
        mock_prelend_client.is_available.return_value = True
        mock_prelend_client.get_metrics.return_value = {
            "total_clicks": 10, "conversions": 0, "cr": None,
            "bot_pct": None, "top_geo": None,
            "shave_suspects": [], "analyst_verdicts": {},
        }

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 10
        assert result["_unreachable"] is False

    def test_api_returns_conversions(self, mock_prelend_client):
        """API возвращает конверсии → conversions и cr корректны."""
        mock_prelend_client.is_available.return_value = True
        mock_prelend_client.get_metrics.return_value = {
            "total_clicks": 20, "conversions": 5, "cr": 0.25,
            "bot_pct": None, "top_geo": None,
            "shave_suspects": [], "analyst_verdicts": {},
        }

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["conversions"] == 5
        assert abs(result["cr"] - 0.25) < 0.0001

    def test_api_returns_bot_pct(self, mock_prelend_client):
        """API возвращает bot_pct → корректно пробрасывается в снапшот."""
        mock_prelend_client.is_available.return_value = True
        mock_prelend_client.get_metrics.return_value = {
            "total_clicks": 4, "conversions": 0, "cr": None,
            "bot_pct": 50.0, "top_geo": None,
            "shave_suspects": [], "analyst_verdicts": {},
        }

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["total_clicks"] == 4
        assert abs(result["bot_pct"] - 50.0) < 0.1

    def test_api_returns_top_geo(self, mock_prelend_client):
        """API возвращает top_geo → отражается в снапшоте."""
        mock_prelend_client.is_available.return_value = True
        mock_prelend_client.get_metrics.return_value = {
            "total_clicks": 3, "conversions": 0, "cr": None,
            "bot_pct": 0.0, "top_geo": "BR",
            "shave_suspects": [], "analyst_verdicts": {},
        }

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["top_geo"] == "BR"

    def test_shave_suspects_normalized(self, mock_prelend_client):
        """shave_suspects из API (список dict) нормализуются в список id-строк."""
        mock_prelend_client.is_available.return_value = True
        mock_prelend_client.get_metrics.return_value = {
            "total_clicks": 5, "conversions": 0, "cr": None,
            "bot_pct": 0.0, "top_geo": "PL",
            "shave_suspects": [{"id": "adv1", "suspected_shave": True}],
            "analyst_verdicts": {},
        }

        from modules.tracking import collect_prelend_snapshot
        result = collect_prelend_snapshot()
        assert result["shave_suspects"] == ["adv1"]


class TestCollectAllAndSave:
    """collect_all_and_save: интеграционный тест сохранения снапшотов в БД."""

    def test_saves_both_snapshots(self, init_database, make_sp_analytics, mock_prelend_client):
        """Оба снапшота сохраняются в metrics_snapshots."""
        from db.metrics import get_latest_snapshot

        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_001": {
                "uploads": {"vk": {"uploaded_at": now_iso, "views": 100, "likes": 5}}
            }
        })
        # Mock уже настроен в conftest: total_clicks=150
        mock_prelend_client.is_available.return_value = True

        from modules.tracking import collect_all_and_save
        collect_all_and_save()

        sp_snap = get_latest_snapshot("ShortsProject")
        pl_snap = get_latest_snapshot("PreLend")

        assert sp_snap is not None
        assert pl_snap is not None
        assert sp_snap["sp_total_views"] == 100
        assert pl_snap["pl_total_clicks"] == 150  # значение из mock

    def test_pl_api_unavailable_sp_still_saved(
        self, init_database, make_sp_analytics, mock_prelend_client
    ):
        """Если PL API недоступен — SP снапшот всё равно сохраняется."""
        from db.metrics import get_latest_snapshot

        now_iso = datetime.now().isoformat()
        make_sp_analytics({
            "vid_x": {
                "uploads": {"vk": {"uploaded_at": now_iso, "views": 50, "likes": 2}}
            }
        })
        mock_prelend_client.is_available.return_value = False

        from modules.tracking import collect_all_and_save
        collect_all_and_save()

        sp_snap = get_latest_snapshot("ShortsProject")
        assert sp_snap is not None
        assert sp_snap["sp_total_views"] == 50
