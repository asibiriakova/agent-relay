-- Runs once, on first container init (empty data volume), alongside the
-- POSTGRES_DB database. Gives the test suite its own database so its
-- fixture (which drops and recreates all tables) never touches dev data.
CREATE DATABASE agent_relay_test OWNER agent_relay;
