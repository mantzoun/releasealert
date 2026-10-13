# About
Release alert is a utility that parses RSS feeds and sends apprise notifications, based on configured rules. The utility keeps a local sqlite database with RSS feed entries it has already processed, so as not to send duplicate norifications.

# Dependencies
* __Docker__\
Download the latest container image from github
```
docker pull ghcr.io/mantzoun/releasealert:latest
```

* __Local execution__\
Install the dependencies in requirements.txt (preferably in a virtual environment).
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

# Configuration
The script requires a config.yml file, located under /data.

User needs to provide this volume containing the config file when running the script from inside the container.

For local execution, the user needs to modify the **DB\_FILE**, **CONFIG\_FILE** and **LOCK\_FILE** constants accordingly.

Additionaly, /data will hold the sqlite database file, feeds.db, which will hold feed data between executions.

The config file has the following entries

* __baseline\_on\_first\_run (optional)__\
boolean.\
If true, no notifications will be sent when running the script for the first time.\
Default is true

* __request\_timeout\_seconds (optional)__\
integer.\
RSS request timeout in seconds.\
Default is 30

* __apprise\_webhooks (optional)__\
List. Contains apprise webhooks to send notifications to. If not set, results are only printed to the console.\
Default is empty.

  * __name__\
    String.\
    Webhook name

  * __url__\
    String.\
    Apprise url

* __feeds__\
List.\
The list of rss feeds to be fetched and examined, along with inclusion/exclusion rules.

  * __name__\
    String.\
    Feed name

  * __feed\_url__\
    String.\
    Feed url

  * __include\_filter (optional)__\
    List.\
    A list of regular expressions. If defined, then a feed entry must match any of the regular expressions in order to trigger a notification.\
    Default is empty.

  * __exclude\_filter (optional)__\
    List.\
    A list of regular expressions. If defined, then feeds matching any of the regular expressions will never trigger a notification.\
    Default is empty.

Check the config.yml in the project repository for an example configuration.

# Execution
First make sure that you have a data folder, containing a valid config.yml file.

* __Docker__
```
docker run --rm -v "./data:/data" ghcr.io/mantzoun/releasealert:latest
```

* __Local execution__
```
python release_alert.py
```

# Notifications and errors
* New matching releases are printed to the console and sent to the configured notification destinations.
* If a notification fails, an error is printed with the webhook name. Other destinations are still attempted.
* Failed notifications are not automatically retried: the corresponding entry URL is still recorded as processed.
* Feed retrieval or parsing failures are reported to the console and cause the script to exit with a nonzero status.
* If a feed fails, successfully retrieved feeds can still be processed during that run.
* The SQLite database stores only processed entry URLs and their associated feed names. Keep this database persistent between executions to avoid processing the same releases repeatedly.
