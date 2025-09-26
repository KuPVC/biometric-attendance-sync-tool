import local_config as config
import datetime
import time
import logging
from logging.handlers import RotatingFileHandler
from pickledb import PickleDB
from zk import ZK, const
import os
import sys
import requests
import json

# Time sync configuration
TIME_SYNC_FREQUENCY = getattr(config, 'TIME_SYNC_FREQUENCY', 1)  # Default: 1 minute for continuous sync
TIME_TOLERANCE_SECONDS = getattr(config, 'TIME_TOLERANCE_SECONDS', 60)  # Sync if difference > 1 minute
ENABLE_TIME_SYNC = getattr(config, 'ENABLE_TIME_SYNC', True)  # Enable/disable time sync

# Google Chat webhook configuration - read from config file
GOOGLE_CHAT_WEBHOOK = getattr(config, 'GOOGLE_CHAT_WEBHOOK', None)
ENABLE_CHAT_NOTIFICATIONS = getattr(config, 'ENABLE_CHAT_NOTIFICATIONS', True)


def setup_time_sync_logger(name, log_file, level=logging.INFO):
    """Setup logger for time sync operations"""
    formatter = logging.Formatter('%(asctime)s\t%(levelname)s\t%(message)s')
    
    handler = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=10)
    handler.setFormatter(formatter)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)
    
    return logger


def send_google_chat_message(message, device_id=None, device_ip=None):
    """Send notification to Google Chat webhook"""
    if not ENABLE_CHAT_NOTIFICATIONS or not GOOGLE_CHAT_WEBHOOK:
        return False
    
    if not GOOGLE_CHAT_WEBHOOK:
        time_sync_logger.warning("Google Chat notifications are enabled but GOOGLE_CHAT_WEBHOOK is not configured in local_config.py")
        return False
    
    try:
        # Create rich message with device details
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if device_id and device_ip:
            full_message = f"🔔 **Biometric Device Alert**\n\n" \
                          f"**Device:** {device_id} ({device_ip})\n" \
                          f"**Time:** {timestamp}\n" \
                          f"**Status:** {message}"
        else:
            full_message = f"🔔 **Biometric System Alert**\n\n" \
                          f"**Time:** {timestamp}\n" \
                          f"**Status:** {message}"
        
        payload = {
            "text": full_message
        }
        
        headers = {
            'Content-Type': 'application/json; charset=UTF-8'
        }
        
        response = requests.post(GOOGLE_CHAT_WEBHOOK, json=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            time_sync_logger.info(f"Google Chat notification sent successfully: {message}")
            return True
        else:
            time_sync_logger.error(f"Failed to send Google Chat notification. Status: {response.status_code}, Response: {response.text}")
            return False
            
    except Exception as e:
        time_sync_logger.error(f"Exception sending Google Chat notification: {str(e)}")
        return False


def check_device_online_status(device):
    """Check if device is online and handle status changes"""
    device_id = device['device_id']
    device_ip = device['ip']
    status_key = f'{device_id}_online_status'
    
    try:
        # Try to connect to device with short timeout
        zk = ZK(device_ip, port=4370, timeout=5)
        conn = zk.connect()
        conn.disconnect()
        
        # Device is online
        previous_status = status.get(status_key)
        
        if previous_status == 'offline' or previous_status is None:
            # Device came back online or first check
            if previous_status == 'offline':
                send_google_chat_message(
                    "✅ Device is back ONLINE", 
                    device_id, 
                    device_ip
                )
                time_sync_logger.info(f"Device {device_id} ({device_ip}): Came back ONLINE")
            
            status.set(status_key, 'online')
            status.save()
        
        return True
        
    except Exception as e:
        # Device is offline
        previous_status = status.get(status_key)
        
        if previous_status != 'offline':
            # Device went offline
            send_google_chat_message(
                "❌ Device is OFFLINE", 
                device_id, 
                device_ip
            )
            time_sync_logger.warning(f"Device {device_id} ({device_ip}): Went OFFLINE - {str(e)}")
            
            status.set(status_key, 'offline')
            status.save()
        
        return False


def get_device_time(ip, port=4370, timeout=30):
    """Get current time from biometric device"""
    zk = ZK(ip, port=port, timeout=timeout)
    conn = None
    device_time = None
    
    try:
        conn = zk.connect()
        device_time = conn.get_time()
        time_sync_logger.info(f"Device {ip}: Current time retrieved - {device_time}")
    except Exception as e:
        time_sync_logger.error(f"Device {ip}: Failed to get time - {str(e)}")
        raise
    finally:
        if conn:
            conn.disconnect()
    
    return device_time


def set_device_time(ip, new_time, port=4370, timeout=30):
    """Set time on biometric device"""
    zk = ZK(ip, port=port, timeout=timeout)
    conn = None
    success = False
    
    try:
        conn = zk.connect()
        
        # Disable device before setting time
        disable_result = conn.disable_device()
        time_sync_logger.info(f"Device {ip}: Disable attempted - {disable_result}")
        
        # Set the new time
        set_result = conn.set_time(new_time)
        time_sync_logger.info(f"Device {ip}: Time set to {new_time} - Result: {set_result}")
        
        # Enable device after setting time
        enable_result = conn.enable_device()
        time_sync_logger.info(f"Device {ip}: Enable attempted - {enable_result}")
        
        success = True
        
    except Exception as e:
        time_sync_logger.error(f"Device {ip}: Failed to set time - {str(e)}")
        raise
    finally:
        if conn:
            conn.disconnect()
    
    return success


def sync_device_time(device):
    """Synchronize time for a single device"""
    device_ip = device['ip']
    device_id = device['device_id']
    
    # First check if device is online
    if not check_device_online_status(device):
        return False
    
    try:
        # Get current system time
        system_time = datetime.datetime.now()
        
        # Get device time
        device_time = get_device_time(device_ip)
        
        if device_time:
            # Calculate time difference
            time_diff = abs((system_time - device_time).total_seconds())
            
            time_sync_logger.debug(f"Device {device_id} ({device_ip}): Time difference - {time_diff:.2f} seconds")
            
            if time_diff > TIME_TOLERANCE_SECONDS:
                time_sync_logger.warning(f"Device {device_id} ({device_ip}): Time difference exceeds tolerance ({TIME_TOLERANCE_SECONDS}s) - Syncing...")
                
                # Set device time to system time
                if set_device_time(device_ip, system_time):
                    time_sync_logger.info(f"Device {device_id} ({device_ip}): Time synchronized successfully")
                    
                    # Send notification for significant time corrections
                    if time_diff > 300:  # 5 minutes
                        send_google_chat_message(
                            f"⏰ Time synchronized (difference was {time_diff:.0f} seconds)", 
                            device_id, 
                            device_ip
                        )
                    
                    # Verify the time was set correctly
                    time.sleep(2)  # Small delay before verification
                    updated_device_time = get_device_time(device_ip)
                    if updated_device_time:
                        verification_diff = abs((system_time - updated_device_time).total_seconds())
                        if verification_diff <= TIME_TOLERANCE_SECONDS:
                            time_sync_logger.info(f"Device {device_id} ({device_ip}): Time sync verification successful")
                            return True
                        else:
                            time_sync_logger.error(f"Device {device_id} ({device_ip}): Time sync verification failed - difference: {verification_diff:.2f}s")
                            return False
                else:
                    time_sync_logger.error(f"Device {device_id} ({device_ip}): Failed to synchronize time")
                    return False
            else:
                time_sync_logger.debug(f"Device {device_id} ({device_ip}): Time is within tolerance, no sync needed")
                return True
        else:
            time_sync_logger.error(f"Device {device_id} ({device_ip}): Could not retrieve device time")
            return False
            
    except Exception as e:
        time_sync_logger.error(f"Device {device_id} ({device_ip}): Exception during time sync - {str(e)}")
        return False


def sync_all_devices():
    """Synchronize time for all configured devices"""
    if not ENABLE_TIME_SYNC:
        time_sync_logger.debug("Time sync is disabled in configuration")
        return
    
    time_sync_logger.debug("Starting time synchronization check for all devices")
    
    success_count = 0
    online_count = 0
    total_devices = len(config.devices)
    
    for device in config.devices:
        try:
            # Check if device is online first
            if check_device_online_status(device):
                online_count += 1
                if sync_device_time(device):
                    success_count += 1
                    # Update status with last sync timestamp
                    status.set(f'{device["device_id"]}_last_time_sync', str(datetime.datetime.now()))
                    status.save()
        except Exception as e:
            time_sync_logger.error(f"Unexpected error syncing device {device['device_id']}: {str(e)}")
    
    if success_count > 0 or online_count != total_devices:
        time_sync_logger.info(f"Time sync completed: {success_count}/{online_count} online devices synchronized successfully ({online_count}/{total_devices} devices online)")
    
    # Update global last sync timestamp
    status.set('last_time_sync_run', str(datetime.datetime.now()))
    status.save()


def should_run_time_sync():
    """Check if it's time to run time synchronization - Now runs continuously"""
    if not ENABLE_TIME_SYNC:
        return False
    
    # For continuous sync, always return True
    # The frequency is now controlled by the sleep interval
    return True


def main_time_sync():
    """Main function for time synchronization"""
    try:
        sync_all_devices()
    except Exception as e:
        time_sync_logger.error(f"Exception in main_time_sync: {str(e)}")


def time_sync_service(sleep_time=60):  # Check every 1 minute for continuous sync
    """Run time sync service in a loop"""
    print("Continuous Time Sync Service Starting...")
    time_sync_logger.info("Continuous Time Sync Service Started")
    print(f"Time tolerance: {TIME_TOLERANCE_SECONDS} seconds (1 minute)")
    print(f"Check interval: {sleep_time} seconds")
    print(f"Google Chat notifications: {'Enabled' if ENABLE_CHAT_NOTIFICATIONS else 'Disabled'}")
    
    # Send startup notification
    if ENABLE_CHAT_NOTIFICATIONS:
        send_google_chat_message(f"🚀 Biometric Time Sync Service Started\n" +
                                f"Monitoring {len(config.devices)} devices with {TIME_TOLERANCE_SECONDS}s tolerance")
    
    while True:
        try:
            main_time_sync()
            time.sleep(sleep_time)  # Check every minute
        except KeyboardInterrupt:
            time_sync_logger.info("Time Sync Service stopped by user")
            print("Time Sync Service stopped")
            if ENABLE_CHAT_NOTIFICATIONS:
                send_google_chat_message("⏹️ Biometric Time Sync Service Stopped")
            break
        except Exception as e:
            time_sync_logger.error(f"Unexpected error in time sync service: {str(e)}")
            print(f"Error in time sync service: {str(e)}")
            time.sleep(sleep_time)


# Initialize logging and status
if not os.path.exists(config.LOGS_DIRECTORY):
    os.makedirs(config.LOGS_DIRECTORY)

time_sync_logger = setup_time_sync_logger(
    'time_sync_logger', 
    os.path.join(config.LOGS_DIRECTORY, 'time_sync.log')
)

status = PickleDB(os.path.join(config.LOGS_DIRECTORY, 'status.json'))


if __name__ == "__main__":
    # You can run this script in different modes:
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "sync":
            # Run sync once and exit
            print("Running one-time sync...")
            sync_all_devices()
            print("Sync completed")
        elif sys.argv[1] == "check":
            # Check time on all devices without syncing
            print("Checking device times and status...")
            for device in config.devices:
                try:
                    print(f"\nDevice {device['device_id']} ({device['ip']}):")
                    
                    # Check if device is online
                    if check_device_online_status(device):
                        print("  Status: ONLINE ✅")
                        device_time = get_device_time(device['ip'])
                        system_time = datetime.datetime.now()
                        diff = abs((system_time - device_time).total_seconds()) if device_time else None
                        print(f"  Device Time: {device_time}")
                        print(f"  System Time: {system_time}")
                        print(f"  Difference: {diff:.2f} seconds" if diff else "  Difference: Unable to calculate")
                        if diff and diff > TIME_TOLERANCE_SECONDS:
                            print(f"  ⚠️  Time difference exceeds tolerance ({TIME_TOLERANCE_SECONDS}s)")
                    else:
                        print("  Status: OFFLINE ❌")
                        
                except Exception as e:
                    print(f"  Error: {str(e)}")
                    
        elif sys.argv[1] == "test":
            # Test Google Chat notification
            print("Testing Google Chat notification...")
            success = send_google_chat_message("🧪 Test notification from Biometric Time Sync Service")
            print(f"Notification sent: {'✅ Success' if success else '❌ Failed'}")
        else:
            print("Usage:")
            print("  python time_sync.py          - Run continuous time sync service")
            print("  python time_sync.py sync     - Run one-time sync")
            print("  python time_sync.py check    - Check device times and status")
            print("  python time_sync.py test     - Test Google Chat notification")
    else:
        # Run continuous service
        time_sync_service()