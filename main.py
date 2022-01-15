#!/usr/bin/env python3
import os
import time
from helpers import INTERVAL_TIME, PROMETHEUS_URL, DRY_RUN, PROMETHEUS_LABEL_MATCH
from helpers import convert_bytes_to_storage, scale_up_pvc, testIfPrometheusIsAccessible, describe_all_pvcs
from helpers import fetch_pvcs_from_prometheus, printHeaderAndConfiguration, calculateBytesToScaleTo
import slack

# Other globals
IN_MEMORY_STORAGE = {}

# Entry point and main application loop
if __name__ == "__main__":

    # This is here to prevent infinite recursion loops on the include of this file from helpers
    import helpers

    # Test if our prometheus URL works before continuing
    testIfPrometheusIsAccessible(PROMETHEUS_URL)

    # TODO: Test k8s access, or just test on-the-fly below?

    # Reporting our configuration to the end-user
    printHeaderAndConfiguration()

    # Our main run loop
    while True:

        # In every loop, fetch all our pvcs state from Kubernetes
        try:
            pvcs_in_kubernetes = describe_all_pvcs(simple=True)
        except Exception as e:
            print("Exception while trying to describe all PVCs")
            print(e)
            time.sleep(INTERVAL_TIME)
            continue

        # Fetch our volume usage from Prometheus
        try:
            pvcs_in_prometheus = fetch_pvcs_from_prometheus(url=PROMETHEUS_URL, label_match=PROMETHEUS_LABEL_MATCH)
        except Exception as e:
            print("Exception while trying to fetch PVC metrics from prometheus")
            print(e)
            time.sleep(INTERVAL_TIME)
            continue

        # Iterate through every item and handle it accordingly
        for item in pvcs_in_prometheus:
            try:
                volume_name = str(item['metric']['persistentvolumeclaim'])
                volume_namespace = str(item['metric']['namespace'])
                volume_description = "{}.{}".format(item['metric']['namespace'], item['metric']['persistentvolumeclaim'])
                volume_used_percent = int(item['value'][1])
                print("Volume {} is {}% in-use".format(volume_description,volume_used_percent), end="")

                # Precursor check to ensure we have info for this pvc in kubernetes object
                if volume_description not in pvcs_in_kubernetes:
                    print("  ERROR: The volume {} was not found in Kubernetes but had metrics in Prometheus.  This may be an old volume, was just deleted, or some random jitter is occurring.  If this continues to occur, please report an bug".format(volume_description))
                else:

                    # Check if we are in an alert condition
                    if volume_used_percent >= pvcs_in_kubernetes[volume_description]['scale_above_percent']:
                        if volume_description in IN_MEMORY_STORAGE:
                            IN_MEMORY_STORAGE[volume_description] = IN_MEMORY_STORAGE[volume_description] + 1
                        else:
                            IN_MEMORY_STORAGE[volume_description] = 1
                        print(" which IS ABOVE {}%".format(pvcs_in_kubernetes[volume_description]['scale_above_percent']))
                        print("  ALERT for {} period(s) which needs to at least {} period(s)".format(IN_MEMORY_STORAGE[volume_description], pvcs_in_kubernetes[volume_description]['scale_after_intervals']))
                        # Check if we are in a possible scale condition
                        if IN_MEMORY_STORAGE[volume_description] >= pvcs_in_kubernetes[volume_description]['scale_after_intervals']:
                            # Check if we recently scaled it, or if there's custom values to override our defaults
                            if pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time'] < int(time.mktime(time.gmtime())):
                                if pvcs_in_kubernetes[volume_description]['last_resized_at'] == 0:
                                    print("  AND we need to scale it, it has never been scaled previously")
                                else:
                                    print("  AND we need to scale it, it last scaled {} seconds ago".format( abs((pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time']) - int(time.mktime(time.gmtime()))) ))

                                resize_to_bytes = calculateBytesToScaleTo(
                                    original_size     = pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'],
                                    scale_up_percent  = pvcs_in_kubernetes[volume_description]['scale_up_percent'],
                                    min_increment     = pvcs_in_kubernetes[volume_description]['scale_up_min_increment'],
                                    max_increment     = pvcs_in_kubernetes[volume_description]['scale_up_max_increment'],
                                    maximum_size      = pvcs_in_kubernetes[volume_description]['scale_up_max_size'],
                                )
                                # TODO: Check if storage class has the ALLOWVOLUMEEXPANSION flag set to true, read the SC from pvcs_in_kubernetes[volume_description]['storage_class']
                                if resize_to_bytes == False:
                                    print("  Error/Exception while trying to determine what to resize to")
                                    continue

                                # Check if we are already at the max volume size (either globally, or this-volume specific)
                                if resize_to_bytes == pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']:
                                    print("  SKIPPING scaling this because we are at the maximum size of {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['scale_up_max_size'])))
                                    continue

                                # Check if we set on this PV we want to ignore the volume autoscaler
                                if pvcs_in_kubernetes[volume_description]['ignore']:
                                    print("  The ignore annotation was set to true, skipping handling this volume")
                                    continue

                                # Check if we are DRY-RUN-ing and won't do anything
                                if DRY_RUN:
                                    print("  DRY RUN, WOULD HAVE RESIZED disk from {} to {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']), convert_bytes_to_storage(resize_to_bytes)))
                                else:
                                    print("  RESIZING disk from {} to {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']), convert_bytes_to_storage(resize_to_bytes)))
                                    if scale_up_pvc(volume_namespace, volume_name, resize_to_bytes):
                                        slack.send("Successfully scaled up `{}` by `{}%` from `{}` to `{}`, it was using more than `{}%` disk space over the last `{} seconds`".format(
                                            volume_description,
                                            pvcs_in_kubernetes[volume_description]['scale_up_percent'],
                                            convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']),
                                            convert_bytes_to_storage(resize_to_bytes),
                                            pvcs_in_kubernetes[volume_description]['scale_above_percent'],
                                            IN_MEMORY_STORAGE[volume_description] * INTERVAL_TIME,
                                        ))
                                    else:
                                        print("  FAILED SCALING UP")
                                        slack.send("FAILED Scaling up `{}` by `{}%` from `{}` to `{}`, it was using more than `{}%` disk space over the last `{} seconds`".format(
                                            volume_description,
                                            pvcs_in_kubernetes[volume_description]['scale_up_percent'],
                                            convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']),
                                            convert_bytes_to_storage(resize_to_bytes),
                                            pvcs_in_kubernetes[volume_description]['scale_above_percent'],
                                            IN_MEMORY_STORAGE[volume_description] * INTERVAL_TIME,
                                        ), severity="error")

                            else:
                                print("  AND need to wait {} seconds to scale".format( abs(pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time']) - int(time.mktime(time.gmtime())) ))
                                print("  HAS desired_size {} and current size {}".format( convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_spec_bytes']), convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'])))

                    else:
                        if volume_description in IN_MEMORY_STORAGE:
                            del IN_MEMORY_STORAGE[volume_description]
                        print(" and is not above {}%".format(pvcs_in_kubernetes[volume_description]['scale_above_percent']))
            except Exception as e:
                print("Exception caught while trying to process record")
                print(item)
                print(e)

        # Wait until our next interval
        time.sleep(INTERVAL_TIME)
