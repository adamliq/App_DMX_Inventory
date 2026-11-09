[api]
base_path = <string>
* Management-port path that the DMX endpoints sit under.
* Default: /services/v2/dmx
pipelines_path = <string>
* Path under base_path for pipelines. Default: pipelines
destinations_path = <string>
* Path under base_path for destinations. Default: destinations
datasets_path = <string>
* Path under base_path for datasets. Default: datasets
offset_param = <string>
* Query parameter used for the pagination offset. Default: offset
count_param = <string>
* Query parameter used for the page size. Default: count
page_size = <positive integer>
* Objects requested per page. Default: 100
max_pages = <positive integer>
* Safety limit on pages fetched per object type per run. Default: 500
timeout_seconds = <positive integer>
* HTTP timeout for DMX calls. Default: 60

[collector]
index = <string>
* Index that snapshot, change and collector events are written to.
* Must match the dmx_index macro. Default: dmx_inventory
credential_realm = <string>
* storage/passwords realm of the service-account token. Default: dmx_inventory
credential_name = <string>
* storage/passwords username of the service-account token. Default: dmx_service_account
allow_session_key_fallback = <boolean>
* If no token is stored, call DMX with the running search's own session key.
* Default: true
lock_ttl_seconds = <positive integer>
* How long a run lock is honoured before it is treated as abandoned. Default: 600
max_batch_bytes = <positive integer>
* Maximum size of one receivers/simple POST. Default: 1000000
