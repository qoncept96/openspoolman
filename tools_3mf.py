import requests
import threading
import zipfile
import tempfile
import xml.etree.ElementTree as ET
import pycurl
import urllib.parse
import os
import re
import time
import io
from datetime import datetime
from config import PRINTER_CODE, PRINTER_IP
from urllib.parse import urlparse
from logger import log

def parse_ftp_listing(line):
    """Parse a line from an FTP LIST command."""
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    return {
        'permissions': parts[0],
        'links': int(parts[1]),
        'owner': parts[2],
        'group': parts[3],
        'size': int(parts[4]),
        'month': parts[5],
        'day': int(parts[6]),
        'time_or_year': parts[7],
        'name': parts[8]
    }

def get_base_name(filename):
    return filename.rsplit('.', 1)[0]

def parse_date(item):
    """Parse the date and time from the FTP listing item."""
    try:
        date_str = f"{item['month']} {item['day']} {item['time_or_year']}"
        return datetime.strptime(date_str, "%b %d %H:%M")
    except ValueError:
        return None

def get_filament_order(file):
    filament_order = {} 
    switch_count = 0 

    for line in file:
        match_filament = re.match(r"^M620 S(\d+)[^;\r\n]*", line.decode("utf-8").strip())
        if match_filament:
            filament = int(match_filament.group(1))
            if filament not in filament_order and int(filament) != 255:
                filament_order[int(filament)] = switch_count
            switch_count += 1

    if len(filament_order) == 0:
       # Keys are M620 S<n> values, which are 0-based and are compared against
       # ams_mapping indices, so a lone filament is 0 - not 1.
       filament_order = {0:0}

    return filament_order

def download3mfFromCloud(url, destFile):
  log("Downloading 3MF file from cloud...")
  # Download the file and save it to the temporary file
  response = requests.get(url)
  response.raise_for_status()
  destFile.write(response.content)

def download3mfFromFTP(filename, destFile):
  # Serialised: see FTP_LOCK. A print's own download must never queue behind or
  # overlap with a bulk one.
  with FTP_LOCK:
    return _download3mfFromFTP(filename, destFile)


def _download3mfFromFTP(filename, destFile):
  log("Downloading 3MF file from FTP...")
  ftp_host = PRINTER_IP
  ftp_user = "bblp"
  ftp_pass = PRINTER_CODE
  local_path = destFile.name  # 🔹 Download into the current directory
  base_name = os.path.basename(filename)
  # Root first. Slicer project files land in the FTP root; /cache/ holds the
  # plate gcode, not the .3mf. Probing /cache/ first meant every download paid a
  # guaranteed miss, and the printer's FTPS server leaves the handle unusable
  # after one (see reconnect_codes below), so that miss costs a full reconnect -
  # minutes per file when fetching in bulk.
  remote_paths = [f"/{base_name}", f"/cache/{base_name}", f"/sdcard/{base_name}"]

  max_retries = 6
  last_err_code = None
  path_count = len(remote_paths)
  # pycurl error codes we react to:
  # 7: could not connect, 28: timeout, 35: SSL connect error,
  # 52: empty reply, 55: send error, 56: recv error.
  # 78: remote file not found, 13: bad PASV/EPSV response, 9: access denied.
  # 78 and 13 are in here because the printer's FTPS server leaves the handle unusable after a
  # miss - reusing it makes the next (valid) path stall until the 120s response timeout.
  reconnect_codes = {7, 13, 28, 35, 52, 55, 56, 78}
  c = setupPycurlConnection(ftp_user, ftp_pass)
  try:
    for attempt in range(1, max_retries + 1):
      path_index = (attempt - 1) % path_count
      remote_path = remote_paths[path_index]
      if not remote_path.startswith("/"):
        remote_path = "/" + remote_path
      encoded_remote_path = urllib.parse.quote(remote_path)
      url = f"ftps://{ftp_host}{encoded_remote_path}"

      log(f"[DEBUG] Attempting file download ({path_index + 1}/{path_count}): {remote_path}") # Log attempted path

      with open(local_path, "wb") as f:
        try:
          c.setopt(c.URL, url)
          c.setopt(c.WRITEDATA, f)
          log(f"[DEBUG] Attempt {attempt}: Starting download of {remote_path}...")
          c.perform()
          f.flush()
          if not zipfile.is_zipfile(local_path):
            log(f"[WARNING] {remote_path} did not return a 3MF archive (a bare directory path yields a listing). Trying next path...")
            continue
          log("[DEBUG] File successfully downloaded!")
          return True
        except pycurl.error as e:
          last_err_code = e.args[0]
          if last_err_code in reconnect_codes:
            log(f"[WARNING] FTP connection error (code {last_err_code}). Reconnecting...")
            try:
              c.close()
            except Exception:
              pass
            c = setupPycurlConnection(ftp_user, ftp_pass)

          if last_err_code in (78, 13):
            if attempt < max_retries:
              log(f"[WARNING] Transient FTP error (code {last_err_code}). Retrying in 5s...")
              time.sleep(5)
              continue
            log("[ERROR] Giving up after max retries for transient FTP errors.")
            break
          if last_err_code == 9:
            log("[DEBUG] Printer denied access to /cache path. Ensure external storage is setup to store print files in printer settings.")
            return False
          log(f"[ERROR] Fatal cURL error {last_err_code}: {e}")
          return False
  finally:
    if c is not None:
      try:
        c.close()
      except Exception:
        pass

  if last_err_code == 78:
    log("[ERROR] File not found after max retries.")
    list_conn = setupPycurlConnection(ftp_user, ftp_pass)
    try:
      for list_path in ("/", "/sdcard/", "/cache/"):
        log(f"[DEBUG] Listing found printer files in {list_path} directory")
        buffer = io.BytesIO()
        list_conn.setopt(list_conn.URL, f"ftps://{ftp_host}{list_path}")
        list_conn.setopt(list_conn.WRITEDATA, buffer)
        list_conn.setopt(list_conn.DIRLISTONLY, True)
        try:
          list_conn.perform()
          log(f"[DEBUG] Directory Listing ({list_path}): {buffer.getvalue().decode('utf-8').splitlines()}")
        except Exception:
          log(f"[ERROR] Could not retrieve directory listing for {list_path}.")
    finally:
      try:
        list_conn.close()
      except Exception:
        pass
  return False

#: Only one FTP transfer at a time. The P1S serves FTPS from the same board that
#: talks MQTT, and it drops off the network for the duration of a transfer - which
#: is why the printer goes "offline" in Home Assistant at every print start. Two
#: overlapping transfers means two outages back to back, or a stall, so the bulk
#: thumbnail recovery can never run on top of a print's own download.
FTP_LOCK = threading.Lock()


def setupPycurlConnection(ftp_user, ftp_pass):
  # Setup shared options for curl connections
  c = pycurl.Curl()

  # Without these a stalled transfer holds the printer for minutes. The printer is
  # unreachable for other things while it serves FTP, so a hung handle is not just a
  # slow download - it is the printer missing from the network.
  c.setopt(c.CONNECTTIMEOUT, 15)
  c.setopt(c.LOW_SPEED_LIMIT, 100)   # bytes/sec ...
  c.setopt(c.LOW_SPEED_TIME, 30)     # ... sustained for this long, or give up

  # 🔹 Setup explicit FTPS connection (like FileZilla)
  
  c.setopt(c.USERPWD, f"{ftp_user}:{ftp_pass}")
  
    
  # 🔹 Enable SSL/TLS
  c.setopt(c.SSL_VERIFYPEER, 0)  # Disable SSL verification
  c.setopt(c.SSL_VERIFYHOST, 0)
    
  # 🔹 Enable passive mode (like FileZilla)
  c.setopt(c.FTP_SSL, c.FTPSSL_ALL)
    
  # 🔹 Enable proper TLS authentication
  c.setopt(c.FTPSSLAUTH, c.FTPAUTH_TLS)

  return c

def download3mfFromLocalFilesystem(path, destFile):
  with open(path, "rb") as src_file:
    destFile.write(src_file.read())

def getMetaDataFrom3mf(url):
  """
  Download a 3MF file from a URL, unzip it, and parse filament usage.

  Args:
      url (str): URL to the 3MF file.

  Returns:
      list[dict]: List of dictionaries with `tray_info_idx` and `used_g`.
  """
  try:
    metadata = {}

    # Create a temporary file
    with tempfile.NamedTemporaryFile(delete_on_close=False,delete=True, suffix=".3mf") as temp_file:
      temp_file_name = temp_file.name
      
      if url.startswith("http"):
        download3mfFromCloud(url, temp_file)
      elif url.startswith("local:"):
        download3mfFromLocalFilesystem(url.replace("local:", ""), temp_file)
      elif url.startswith(("file://", "ftp://", "ftps://")):
        # OrcaSlicer reports the model as "ftp://Name.gcode.3mf" (two slashes), which parses the
        # filename into netloc and leaves path empty. Bambu Studio sends "ftp:///Name.gcode.3mf".
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or parsed.netloc
        if not download3mfFromFTP(filename, temp_file):
          return {}
      else:
        if not download3mfFromFTP(url.rpartition('/')[-1], temp_file): # Pull just filename to clear out any unexpected paths
          return {}
      
      temp_file.close()
      metadata["model_path"] = url

      parsed_url = urlparse(url)
      metadata["file"] = os.path.basename(parsed_url.path) or parsed_url.netloc

      log(f"3MF file downloaded and saved as {temp_file_name}.")

      # Unzip the 3MF file
      with zipfile.ZipFile(temp_file_name, 'r') as z:
        # Check for the Metadata/slice_info.config file
        slice_info_path = "Metadata/slice_info.config"
        if slice_info_path in z.namelist():
          with z.open(slice_info_path) as slice_info_file:
            # Parse the XML content of the file
            tree = ET.parse(slice_info_file)
            root = tree.getroot()

            # Extract id and used_g from each filament
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <config>
              <header>
                <header_item key="X-BBL-Client-Type" value="slicer"/>
                <header_item key="X-BBL-Client-Version" value="01.10.01.50"/>
              </header>
              <plate>
                <metadata key="index" value="1"/>
                <metadata key="printer_model_id" value="N2S"/>
                <metadata key="nozzle_diameters" value="0.4"/>
                <metadata key="timelapse_type" value="0"/>
                <metadata key="prediction" value="5450"/>
                <metadata key="weight" value="26.91"/>
                <metadata key="outside" value="false"/>
                <metadata key="support_used" value="false"/>
                <metadata key="label_object_enabled" value="true"/>
                <object identify_id="930" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1030" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1130" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1230" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1330" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1430" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1530" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1630" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1730" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1830" name="FILENAME.3mf" skipped="false" />
                <object identify_id="1930" name="FILENAME.3mf" skipped="false" />
                <object identify_id="2030" name="FILENAME.3mf" skipped="false" />
                <object identify_id="2130" name="FILENAME.3mf" skipped="false" />
                <object identify_id="2230" name="FILENAME.3mf" skipped="false" />
                <filament id="1" tray_info_idx="GFL99" type="PLA" color="#0DFF00" used_m="6.79" used_g="20.26" />
                <filament id="2" tray_info_idx="GFL99" type="PLA" color="#000000" used_m="0.72" used_g="2.15" />
                <filament id="6" tray_info_idx="GFL99" type="PLA" color="#0DFF00" used_m="1.20" used_g="3.58" />
                <filament id="7" tray_info_idx="GFL99" type="PLA" color="#000000" used_m="0.31" used_g="0.92" />
                <warning msg="bed_temperature_too_high_than_filament" level="1" error_code ="1000C001"  />
              </plate>
            </config>
            """
            
            for meta in root.findall(".//plate/metadata"):
              if meta.attrib.get("key") == "index":
                  metadata["plateID"] = meta.attrib.get("value", "")

            # slice_info gives every filament the slicer's own index, and so does everything
            # downstream: the gcode's M620 S<n>, the printer's ams_mapping, and the
            # filament_usage rows keyed off these ids. Renumbering them 1..N by position only
            # agreed with the rest when a plate happened to start at filament 1 - print 69 was
            # sliced on filament 2, so the tracker billed slot 2 against a row written as slot
            # 1, matched nothing, and the print went uncharged.
            usage = {}
            filaments = {}
            plate_id = metadata.get("plateID")
            plates = [
              plate for plate in root.findall(".//plate")
              if any(meta.attrib.get("key") == "index" and meta.attrib.get("value") == plate_id
                     for meta in plate.findall("./metadata"))
            ] or root.findall(".//plate")
            next_id = 1
            for plate in plates:
              for filament in plate.findall(".//filament"):
                used_g = filament.attrib.get("used_g")
                try:
                  filamentId = int(filament.attrib.get("id"))
                except (TypeError, ValueError):
                  # A 3MF missing the attribute should still produce rows to bill against.
                  filamentId = next_id
                next_id = filamentId + 1

                usage[filamentId] = used_g
                filaments[filamentId] = {"id": filamentId,
                                         "tray_info_idx": filament.attrib.get("tray_info_idx"),
                                         "type":filament.attrib.get("type"),
                                         "color": filament.attrib.get("color"),
                                         "used_g": used_g,
                                         "used_m":filament.attrib.get("used_m")}

            metadata["filaments"] = filaments
            metadata["usage"] = usage
        else:
          log(f"File '{slice_info_path}' not found in the archive.")
          return {}

        metadata["image"] = time.strftime('%Y%m%d%H%M%S') + ".png"

        with z.open("Metadata/plate_"+metadata["plateID"]+".png") as source_file:
          with open(os.path.join(os.getcwd(), 'static', 'prints', metadata["image"]), 'wb') as target_file:
              target_file.write(source_file.read())

        # Check for the Metadata/slice_info.config file
        gcode_path = "Metadata/plate_"+metadata["plateID"]+".gcode"
        metadata["gcode_path"] = gcode_path
        if gcode_path in z.namelist():
          with z.open(gcode_path) as gcode_file:
            metadata["filamentOrder"] =  get_filament_order(gcode_file)

        log(metadata)

        return metadata

  except requests.exceptions.RequestException as e:
    log(f"Error downloading file: {e}")
    return {}
  except zipfile.BadZipFile:
    log("The downloaded file is not a valid 3MF archive.")
    return {}
  except ET.ParseError:
    log("Error parsing the XML file.")
    return {}
  except Exception as e:
    log(f"An unexpected error occurred: {e}")
    return {}
