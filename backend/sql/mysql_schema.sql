-- ============================================================
-- 基于 Spark 的航班延误预测与航线网络态势可视化系统
-- MySQL 8.0 建表脚本（由 scripts/gen_sql.py 从 ORM 元数据生成，勿手改）
-- 用法：mysql -u root -p < mysql_schema.sql
-- ============================================================
CREATE DATABASE IF NOT EXISTS flight_delay
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE flight_delay;

CREATE TABLE ai_chat_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts VARCHAR(19) NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	question TEXT NOT NULL, 
	answer TEXT NOT NULL, 
	context_json TEXT NOT NULL, 
	model VARCHAR(64) NOT NULL, 
	degraded INTEGER NOT NULL COMMENT '1=未配置 API Key 走本地摘要', 
	latency_ms INTEGER NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ai_chat_log';
CREATE INDEX ix_ai_chat_log_ts ON ai_chat_log (ts);

CREATE TABLE case_bookmark (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	flight_sk VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	note TEXT NOT NULL, 
	tags VARCHAR(256) NOT NULL, 
	created_by VARCHAR(64) NOT NULL, 
	created_at VARCHAR(19) NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='case_bookmark';
CREATE INDEX ix_case_bookmark_flight_sk ON case_bookmark (flight_sk);

CREATE TABLE model_version (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	model_ver VARCHAR(64) NOT NULL COMMENT '如 lgbm_cls15_20261117_120000', 
	task VARCHAR(32) NOT NULL COMMENT 'cls15(二分类)/level3(三级分类)/reg(分钟回归)', 
	algo VARCHAR(32) NOT NULL, 
	feature_set TEXT NOT NULL COMMENT '逗号分隔特征名', 
	train_range VARCHAR(32) NOT NULL COMMENT 'yyyy-MM~yyyy-MM', 
	test_range VARCHAR(32) NOT NULL, 
	params_json TEXT NOT NULL, 
	metrics_json TEXT NOT NULL, 
	artifact_path VARCHAR(512) NOT NULL, 
	is_online INTEGER NOT NULL, 
	seed INTEGER NOT NULL, 
	created_by VARCHAR(64) NOT NULL, 
	created_at VARCHAR(19) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (model_ver)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='model_version';
CREATE INDEX ix_model_version_task ON model_version (task);
CREATE INDEX ix_model_version_is_online ON model_version (is_online);

CREATE TABLE ods_file_manifest (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	dataset VARCHAR(32) NOT NULL, 
	period VARCHAR(16) NOT NULL, 
	file_name VARCHAR(256) NOT NULL, 
	rel_path VARCHAR(512) NOT NULL, 
	file_md5 VARCHAR(32) NOT NULL, 
	file_bytes BIGINT NOT NULL, 
	row_count BIGINT NOT NULL, 
	verified INTEGER NOT NULL COMMENT '1=已对账', 
	verified_at VARCHAR(19) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_manifest_key UNIQUE (dataset, period, file_name)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ods_file_manifest';
CREATE INDEX ix_ods_file_manifest_dataset ON ods_file_manifest (dataset);
CREATE INDEX ix_ods_file_manifest_period ON ods_file_manifest (period);

CREATE TABLE ods_ingest_audit (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	job_id VARCHAR(48) NOT NULL, 
	dataset VARCHAR(32) NOT NULL, 
	period VARCHAR(16) NOT NULL, 
	file_name VARCHAR(256) NOT NULL, 
	file_md5 VARCHAR(32) NOT NULL, 
	file_bytes BIGINT NOT NULL, 
	raw_rows BIGINT NOT NULL COMMENT '源文件行数（含表头另计）', 
	ods_rows BIGINT NOT NULL, 
	drop_rows BIGINT NOT NULL, 
	drop_reason VARCHAR(256) NOT NULL, 
	channel VARCHAR(4) NOT NULL COMMENT 'A 批处理/B 控制面/C 页面直传', 
	mode VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	err_code VARCHAR(32) NOT NULL, 
	operator VARCHAR(64) NOT NULL, 
	started_at VARCHAR(19) NOT NULL, 
	finished_at VARCHAR(19) NOT NULL, 
	elapsed_sec FLOAT NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ods_ingest_audit';
CREATE INDEX ix_ods_ingest_audit_status ON ods_ingest_audit (status);
CREATE INDEX ix_ods_ingest_audit_period ON ods_ingest_audit (period);
CREATE INDEX ix_ods_ingest_audit_job_id ON ods_ingest_audit (job_id);
CREATE INDEX ix_ods_ingest_audit_dataset ON ods_ingest_audit (dataset);

CREATE TABLE ods_ingest_failed (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	job_id VARCHAR(48) NOT NULL, 
	dataset VARCHAR(32) NOT NULL, 
	period VARCHAR(16) NOT NULL, 
	file_name VARCHAR(256) NOT NULL, 
	err_code VARCHAR(32) NOT NULL, 
	err_msg TEXT NOT NULL, 
	attempts INTEGER NOT NULL, 
	ignored INTEGER NOT NULL COMMENT '1=人工标记忽略', 
	first_fail_at VARCHAR(19) NOT NULL, 
	last_fail_at VARCHAR(19) NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ods_ingest_failed';
CREATE INDEX ix_ods_ingest_failed_job_id ON ods_ingest_failed (job_id);
CREATE INDEX ix_ods_ingest_failed_dataset ON ods_ingest_failed (dataset);

CREATE TABLE ods_staging_file (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	staging_id VARCHAR(48) NOT NULL, 
	dataset VARCHAR(32) NOT NULL, 
	file_name VARCHAR(256) NOT NULL, 
	file_path VARCHAR(512) NOT NULL, 
	file_md5 VARCHAR(32) NOT NULL, 
	file_bytes BIGINT NOT NULL, 
	uploaded_by VARCHAR(64) NOT NULL, 
	uploaded_at VARCHAR(19) NOT NULL, 
	promoted INTEGER NOT NULL, 
	promoted_at VARCHAR(19) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (staging_id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ods_staging_file';
CREATE INDEX ix_ods_staging_file_dataset ON ods_staging_file (dataset);

CREATE TABLE sys_alert_event (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	rule_id INTEGER NOT NULL, 
	rule_name VARCHAR(128) NOT NULL, 
	triggered_at VARCHAR(19) NOT NULL, 
	level VARCHAR(16) NOT NULL, 
	scope VARCHAR(128) NOT NULL, 
	observed FLOAT NOT NULL, 
	message VARCHAR(512) NOT NULL, 
	acked BOOL NOT NULL, 
	acked_by VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='sys_alert_event';
CREATE INDEX ix_sys_alert_event_triggered_at ON sys_alert_event (triggered_at);
CREATE INDEX ix_sys_alert_event_rule_id ON sys_alert_event (rule_id);

CREATE TABLE sys_alert_rule (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	name VARCHAR(128) NOT NULL, 
	metric VARCHAR(64) NOT NULL COMMENT 'ontime_rate/delay_rate/cancel_rate/avg_delay/rows_dropped', 
	comparator VARCHAR(8) NOT NULL COMMENT 'lt/gt/le/ge', 
	threshold FLOAT NOT NULL, 
	scope_type VARCHAR(16) NOT NULL COMMENT 'airport/route/global/job', 
	scope_value VARCHAR(128) NOT NULL COMMENT 'ATL / ATL-ORD / *', 
	level VARCHAR(16) NOT NULL COMMENT 'info/warning/critical', 
	enabled BOOL NOT NULL, 
	created_by VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='sys_alert_rule';

CREATE TABLE sys_audit_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	ts VARCHAR(19) NOT NULL COMMENT 'yyyy-MM-dd HH:mm:ss', 
	username VARCHAR(64) NOT NULL, 
	`role` VARCHAR(16) NOT NULL, 
	action VARCHAR(64) NOT NULL COMMENT 'login/ingest/model_online/...', 
	resource VARCHAR(128) NOT NULL, 
	detail TEXT NOT NULL, 
	ip VARCHAR(64) NOT NULL, 
	trace_id VARCHAR(32) NOT NULL, 
	result VARCHAR(16) NOT NULL, 
	PRIMARY KEY (id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='sys_audit_log';
CREATE INDEX ix_sys_audit_log_action ON sys_audit_log (action);
CREATE INDEX ix_sys_audit_log_ts ON sys_audit_log (ts);
CREATE INDEX ix_sys_audit_log_username ON sys_audit_log (username);

CREATE TABLE sys_job_run (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	job_id VARCHAR(48) NOT NULL, 
	job_name VARCHAR(64) NOT NULL COMMENT 'ingest_bts/etl_dwd/...', 
	dataset VARCHAR(32) NOT NULL, 
	period VARCHAR(16) NOT NULL COMMENT 'yyyy-MM 或 yyyy；区间用逗号', 
	mode VARCHAR(16) NOT NULL COMMENT 'full/incremental', 
	engine VARCHAR(16) NOT NULL COMMENT 'spark/pandas', 
	status VARCHAR(16) NOT NULL COMMENT 'queued/running/success/failed/blocked/cancelled', 
	progress FLOAT NOT NULL COMMENT '0-100', 
	current_file VARCHAR(256) NOT NULL, 
	total_files INTEGER NOT NULL, 
	done_files INTEGER NOT NULL, 
	rows_in BIGINT NOT NULL, 
	rows_out BIGINT NOT NULL, 
	drop_rows BIGINT NOT NULL, 
	drop_reason TEXT NOT NULL COMMENT 'JSON: {E01: 123}', 
	stages TEXT NOT NULL COMMENT 'JSON: [{stage,status,elapsed_sec}]', 
	err_code VARCHAR(32) NOT NULL, 
	err_msg TEXT NOT NULL, 
	operator VARCHAR(64) NOT NULL, 
	started_at VARCHAR(19) NOT NULL, 
	finished_at VARCHAR(19) NOT NULL, 
	elapsed_sec FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (job_id)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='sys_job_run';
CREATE INDEX ix_sys_job_run_status ON sys_job_run (status);
CREATE INDEX ix_sys_job_run_job_name ON sys_job_run (job_name);
CREATE INDEX ix_sys_job_run_dataset ON sys_job_run (dataset);
CREATE INDEX ix_job_dataset_period ON sys_job_run (dataset, period);

CREATE TABLE sys_user (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	username VARCHAR(64) NOT NULL COMMENT '登录名', 
	password_hash VARCHAR(255) NOT NULL COMMENT 'pbkdf2 加盐哈希', 
	`role` VARCHAR(16) NOT NULL COMMENT 'guest/ops/analyst/admin', 
	display_name VARCHAR(64) NOT NULL COMMENT '显示名', 
	email VARCHAR(128) NOT NULL, 
	scope_airports VARCHAR(512) NOT NULL COMMENT '数据范围预留字段，当前不启用', 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_login_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (username)
)
  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='sys_user';

