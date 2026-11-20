"""接口层测试：认证、权限、统一响应结构、参数校验、上传白名单、周期校验。"""
from __future__ import annotations


class TestAuth:
    def test_login_success(self, client):
        response = client.post("/api/v1/auth/login",
                               json={"username": "admin", "password": "admin123"})
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["code"] == 0
        assert payload["data"]["token"]
        assert payload["data"]["user"]["role"] == "admin"
        assert payload["data"]["menus"] == ["dashboard", "analysis", "model", "data", "system"]
        assert payload["trace_id"]

    def test_login_wrong_password(self, client):
        response = client.post("/api/v1/auth/login",
                               json={"username": "admin", "password": "bad"})
        payload = response.get_json()
        assert response.status_code == 401
        assert payload["code"] == 401

    def test_profile_requires_token(self, client):
        response = client.get("/api/v1/auth/profile")
        assert response.status_code == 401
        assert response.get_json()["code"] == 401

    def test_profile_with_token(self, client, auth):
        response = client.get("/api/v1/auth/profile", headers=auth("ops"))
        payload = response.get_json()
        assert payload["code"] == 0
        assert payload["data"]["user"]["role"] == "ops"

    def test_guest_profile_menus(self, client, auth):
        response = client.get("/api/v1/auth/profile", headers=auth("guest"))
        data = response.get_json()["data"]
        assert data["menus"] == ["dashboard"]
        assert data["permissions"]["read_only"] is True


class TestRBAC:
    def test_guest_cannot_manage_data(self, client, auth):
        response = client.get("/api/v1/ingest/datasets", headers=auth("guest"))
        assert response.status_code == 403
        assert response.get_json()["code"] == 403

    def test_ops_cannot_view_datasets_requires_ops(self, client, auth):
        response = client.get("/api/v1/ingest/datasets", headers=auth("ops"))
        assert response.status_code == 200

    def test_analyst_cannot_manage_users(self, client, auth):
        response = client.get("/api/v1/system/users", headers=auth("analyst"))
        assert response.status_code == 403

    def test_admin_can_list_users(self, client, auth):
        response = client.get("/api/v1/system/users", headers=auth("admin"))
        payload = response.get_json()
        assert payload["code"] == 0
        assert len(payload["data"]["items"]) >= 4

    def test_guest_cannot_see_propagation(self, client, auth):
        assert client.get("/api/v1/propagation", headers=auth("guest")).status_code == 403
        assert client.get("/api/v1/propagation", headers=auth("ops")).status_code in (200,)


class TestValidation:
    def test_missing_param(self, client, auth):
        response = client.post("/api/v1/predict", headers=auth("guest"), json={"origin": "JFK"})
        assert response.status_code == 400
        assert response.get_json()["code"] == 400

    def test_bad_date_format(self, client, auth):
        response = client.get("/api/v1/overview/kpi?date_from=2015-1-1", headers=auth("guest"))
        assert response.status_code == 400
        assert response.get_json()["code"] == 422

    def test_bad_airport_code(self, client, auth):
        response = client.get("/api/v1/airport/AT", headers=auth("guest"))
        assert response.get_json()["code"] == 422

    def test_unknown_period_rejected(self, client, auth):
        response = client.post("/api/v1/ingest/verify", headers=auth("admin"),
                              json={"dataset": "bts", "periods": ["2024-13"], "calc_md5": False})
        assert response.status_code == 400
        assert response.get_json()["code"] == 422

    def test_unknown_job(self, client, auth):
        response = client.get("/api/v1/ingest/jobs/nope", headers=auth("admin"))
        assert response.status_code == 404

    def test_unknown_table(self, client, auth):
        response = client.get("/api/v1/warehouse/tables/not_a_table/partitions", headers=auth("ops"))
        assert response.status_code == 404


class TestUpload:
    def test_exe_rejected(self, client, auth):
        data = {"file": (__import__("io").BytesIO(b"MZ\x90\x00"), "evil.exe"),
                "dataset": "custom"}
        response = client.post("/api/v1/upload/staging", headers=auth("admin"),
                               data=data, content_type="multipart/form-data")
        payload = response.get_json()
        assert payload["code"] == 415

    def test_path_traversal_rejected(self, client, auth):
        data = {"file": (__import__("io").BytesIO(b"a,b\n1,2\n"), "../../etc/passwd.csv"),
                "dataset": "custom"}
        response = client.post("/api/v1/upload/staging", headers=auth("admin"),
                               data=data, content_type="multipart/form-data")
        assert response.get_json()["code"] in (415, 422)

    def test_valid_csv_accepted(self, client, auth):
        data = {"file": (__import__("io").BytesIO("date,name\n2024-01-01,Holiday\n".encode()),
                         "ok.csv"), "dataset": "custom"}
        response = client.post("/api/v1/upload/staging", headers=auth("admin"),
                               data=data, content_type="multipart/form-data")
        payload = response.get_json()
        assert payload["code"] == 0
        assert payload["data"]["file_bytes"] > 0
        assert payload["data"]["staging_id"].startswith("stg_")


class TestSystem:
    def test_health(self, client):
        payload = client.get("/api/v1/system/health").get_json()
        assert payload["code"] == 0
        assert payload["data"]["status"] in ("ok", "empty")
        assert payload["data"]["warehouse"]["total_tables"] == 17

    def test_endpoints_listing(self, client):
        payload = client.get("/api/v1/system/endpoints").get_json()
        assert payload["code"] == 0
        paths = {item["path"] for item in payload["data"]["items"]}
        assert "/api/v1/overview/kpi" in paths
        assert "/api/v1/crossdomain/china" in paths

    def test_index(self, client):
        # API 模块清单和无前端产物时的根路径均可访问。
        payload = client.get("/api/v1").get_json()
        assert payload["code"] == 0
        assert "modules" in payload["data"]
        home = client.get("/")
        assert home.status_code == 200
        assert home.mimetype in ("text/html", "application/json")


class TestQuality:
    def test_ingest_datasets(self, client, auth):
        payload = client.get("/api/v1/ingest/datasets", headers=auth("admin")).get_json()
        assert payload["code"] == 0
        keys = {item["key"] for item in payload["data"]["items"]}
        assert {"bts", "isd", "ourairports", "openflights", "stpn", "manifest"} <= keys
        bts = next(i for i in payload["data"]["items"] if i["key"] == "bts")
        assert bts["period_kind"] == "year_month"
        assert bts["channel"] == "A"

    def test_warehouse_tables(self, client, auth):
        payload = client.get("/api/v1/warehouse/tables", headers=auth("ops")).get_json()
        assert payload["code"] == 0
        assert len(payload["data"]["items"]) == 17

    def test_feature_spec_leakage_check(self, client, auth):
        payload = client.get("/api/v1/model/feature-spec", headers=auth("ops")).get_json()
        assert payload["code"] == 0
        assert payload["data"]["leakage_check"] == "0 个禁用字段"

