import requests
import json
from uuid import uuid4
from django.http import JsonResponse

def request_payment(request):
    # Define the endpoint for requesting payment
    endpoint = "https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay"

    # Define your API credentials
    primary_key = "a166d4787fc9420e91e128349a4c0ec7"
    secondary_key = "e9b6963d0458434e9e692f4043def610"
    access_token = "your_access_token"
    target_environment = "sandbox"
    reference_id = str(uuid4())  # Generate a unique reference ID for the transaction

    # Define the payment request payload
    payload = {
        "amount": "100",  # Specify the amount to be paid
        "currency": "GHC",  # Specify the currency
        "externalId": "123456789",  # Specify your unique reference ID for the transaction
        "payer": {
            "partyIdType": "MSISDN",
            "partyId": "0551751552"  # Specify the phone number of the payer
        },
        "payerMessage": "Payment request message",
        "payeeNote": "Note for payee"
    }

    # Set up headers with access token and primary key
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Ocp-Apim-Subscription-Key": primary_key,
        "Content-Type": "application/json",
        "X-Reference-Id": reference_id,
        "X-Target-Environment": target_environment
    }

    print("Initiating payment request...")

    # Make the POST request to initiate the payment
    response = requests.post(endpoint, json=payload, headers=headers)

    # Log the request payload
    print("Request payload:", payload)

    # Log the response status
    print("Response status code:", response.status_code)

    # Check if the request was successful
    if response.status_code == 202:
        # Payment request was successful, log the success
        print("Payment request successful.")
        return JsonResponse({"status": "success", "data": response.json()}, status=202)
    elif response.status_code == 400:
        # Bad request, log and return the error message
        print("Bad request, invalid data was sent.")
        return JsonResponse({"status": "error", "message": "Bad request, invalid data was sent"}, status=400)
    elif response.status_code == 409:
        # Conflict, duplicated reference id, log and return the error message
        print("Conflict, duplicated reference id.")
        return JsonResponse({"status": "error", "message": "Conflict, duplicated reference id"}, status=409)
    else:
        # Other error, log and return the error message
        print("Internal server error.")
        return JsonResponse({"status": "error", "message": "Internal server error"}, status=500)
