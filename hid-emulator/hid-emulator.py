"""Emulate input commands and post this command to a corresponding workbench endpoint."""

import logging
from datetime import datetime

import requests

logging.basicConfig(format="%(levelname)s:%(asctime)s:%(message)s", level=logging.INFO)
API_ENDPOINT = "http://127.0.0.1:5000/workbench/hid-event"


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


while True:
    try:
        print(
            "Select emulated action (1/2): \n",
            f"{bcolors.OKGREEN}1. Put ID card on the RFID scanner.\n{bcolors.ENDC}",
            f"{bcolors.OKBLUE}2. Scan a sample barcode with a barcode scanner.{bcolors.ENDC}",
        )
        action: str = input()
        if action == "1":
            json_event = {
                "string": "1111111111",
                "name": "Sample RFID Scanner",
                "timestamp": str(datetime.timestamp(datetime.now())),
                "info": {},
            }
            requests.post(url=API_ENDPOINT, json=json_event)
            logging.info(f"Event relayed to endpoint {API_ENDPOINT}")
        elif action == "2":
            print("Insert the device unique code")
            code = input()
            json_event = {
                "string": code,
                "name": "Sample Barcode Scanner",
                "timestamp": str(datetime.timestamp(datetime.now())),
                "info": {},
            }
            requests.post(url=API_ENDPOINT, json=json_event)
            logging.info(f"Event relayed to endpoint {API_ENDPOINT}")
        else:
            print(f"{bcolors.FAIL}Input 1 or 2{bcolors.ENDC}")
    except Exception as e:
        print(f"{bcolors.FAIL}Error: {e}{bcolors.ENDC}")
