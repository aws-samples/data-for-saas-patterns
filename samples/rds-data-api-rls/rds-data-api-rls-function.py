"""
CREATE TABLE tenant ( tenant_id integer PRIMARY KEY, tenant_name text, account_balance numeric );
INSERT INTO tenant VALUES (1, 'Tenant1', 50000), (2, 'Tenant2', 60000), (3, 'Tenant3', 40000);
CREATE POLICY tenant_policy ON tenant USING (tenant_id = current_setting('tenant.id')::integer);
ALTER TABLE tenant enable row level security;
"""
import boto3

cluster_arn = '<cluster arn'
secret_arn = '<secret arn>'

rdsData = boto3.client('rds-data')

db_name = 'postgres'

def get_tenant_id_from_context():
    return 2;

param1 = {'name':'id', 'value':{'longValue': get_tenant_id_from_context()}}
paramSet = [param1]

""" 
CREATE OR REPLACE FUNCTION get_tenant_data(p_tenant_id integer) 
  RETURNS SETOF text AS
$func$
BEGIN
   EXECUTE format('SET "tenant.id" = %s', p_tenant_id);
   RETURN QUERY
   SELECT tenant_name
   FROM tenant;
END
$func$  LANGUAGE plpgsql;

SET ROLE to app_user;

SELECT get_tenant_data(2);
"""
response = rdsData.execute_statement(resourceArn=cluster_arn,
                                      secretArn=secret_arn,
                                      database=db_name,
                                      sql='select get_tenant_data(:id::integer)',
                                      parameters = paramSet)

print(response['records'])