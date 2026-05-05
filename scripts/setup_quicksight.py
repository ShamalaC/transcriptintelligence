"""
This script is no longer needed.

QuickSight dataset and dashboard creation is now fully automated by `cdk deploy`.
The CDK Custom Resource (lambda/qs_setup/handler.py) runs during CloudFormation
deployment and creates/updates the dataset and dashboard automatically.

To configure which QuickSight user owns the dashboard, set `qs_user` in cdk.json:

    "context": {
        "qs_user": "default/your-quicksight-username",
        ...
    }

Or pass it at deploy time:

    cdk deploy -c qs_user=default/your-quicksight-username

Then run `cdk deploy` -- everything is handled automatically.
"""
import sys
print(__doc__)
sys.exit(0)
