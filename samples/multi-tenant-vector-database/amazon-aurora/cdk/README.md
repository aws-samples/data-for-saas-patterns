# Deploy Amazon Aurora using CDK

Amazon Aurora PostgreSQL is one of the prerequisite for running the sample notebooks. You can make use of this CDK to provision an Aurora PostgreSQL cluster. 


## Requirements

* [Create an AWS account](https://portal.aws.amazon.com/gp/aws/developer/registration/index.html) if you do not already have one and log in. The IAM user that you use must have sufficient permissions to make necessary AWS service calls and manage AWS resources.
* [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) installed and configured
* [Git Installed](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
* [Node and NPM](https://nodejs.org/en/download/) installed.
* [AWS CDK](https://docs.aws.amazon.com/cdk/latest/guide/cli.html) installed and configured

## Deployment Instructions

1. Clone the GitHub repository:
    ``` 
    git clone https://github.com/aws-samples/data-for-saas-patterns.git
    ```
2. Change directory to the cdk project :
    ```
    cd data-for-saas-patterns/multi-tenant-vector-database/amazon-aurora/cdk
    ```
3. Install dependencies:
    ```
    npm install
    ```
4. Configure AWS CDK to bootstrap the AWS account:
    ```
    cdk bootstrap <account-id>/<region>
    ```
5. From the command line, use AWS CDK to deploy the stack: 
    ```
    cdk deploy
    ```
    
6. Note the outputs from the CDK deployment process. These contain resources ARNs needed for the notebooks.

7. Finally after testing all the samples, you can remove the Aurora PostgreSQL cluster using the destroy command.
    ```
    cdk destroy
    ```

