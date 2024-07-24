"""
CREATE TABLE tenant ( tenant_id integer PRIMARY KEY, tenant_name text, account_balance numeric );
INSERT INTO tenant VALUES (1, 'Tenant1', 50000), (2, 'Tenant2', 60000), (3, 'Tenant3', 40000);
CREATE POLICY tenant_policy ON tenant USING (tenant_id = current_setting('tenant.id')::integer);
ALTER TABLE tenant enable row level security;
"""
import boto3

cluster_arn = '<cluster arn>'
secret_arn = '<secret arn>'

rdsData = boto3.client('rds-data')

db_name = 'postgres'

def get_tenant_id_from_context():
    return 2;

tr = rdsData.begin_transaction(
     resourceArn = cluster_arn,
     secretArn = secret_arn,
     database = db_name)

rdsData.execute_statement(resourceArn=cluster_arn,
                          secretArn=secret_arn,
                          database=db_name,
                          sql='set tenant.id = {0}'.format(get_tenant_id_from_context()),
                          transactionId = tr['transactionId'])

response = rdsData.execute_statement(resourceArn=cluster_arn,
                                      secretArn=secret_arn,
                                      database=db_name,
                                      sql='select tenant_name from tenant',
                                      transactionId = tr['transactionId'])

cr = rdsData.commit_transaction(
     resourceArn = cluster_arn,
     secretArn = secret_arn,
     transactionId = tr['transactionId'])