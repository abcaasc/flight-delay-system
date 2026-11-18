-- 已部署 MySQL 8.0 实例升级；全新实例使用 mysql_schema.sql。
-- 保留现有数据，只扩展特征白名单字段容量。
ALTER TABLE model_version MODIFY COLUMN feature_set TEXT;
