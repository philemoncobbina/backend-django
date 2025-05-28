# views.py
import requests
from django.http import JsonResponse
import traceback

def request_money(request):
    client_id = "ydabrynb"
    client_secret = "knlxmpsr"
    api_url = "https://smsc.hubtel.com/v1/messages/send?clientsecret=knlxmpsr&clientid=ydabrynb&from=Philemon20&to=233531004645&content=This+Is+A+Test+Message"

    # Fixed parameters
    mobile_number = "233531004645"  # Fixed phone number
    amount = 1  # Fixed amount

    # Other parameters
    title = request.POST.get('title')
    description = request.POST.get('description')
    client_reference = request.POST.get('client_reference')
    callback_url = request.POST.get('callback_url')
    cancellation_url = request.POST.get('cancellation_url')
    return_url = request.POST.get('return_url')
    logo = request.POST.get('logo')

    # Construct payload
    payload = {
        "amount": amount,
        "title": title,
        "description": description,
        "clientReference": client_reference,
        "callbackUrl": callback_url,
        "cancellationUrl": cancellation_url,
        "returnUrl": return_url,
        "logo": logo
    }

    try:
        # Make the API call
        response = requests.post(api_url + mobile_number, json=payload, auth=(client_id, client_secret))

        # Handle response
        if response.status_code == 200:
            data = response.json()
            return JsonResponse(data)
        else:
            return JsonResponse({"error": "Failed to request money"}, status=500)
    except Exception as e:
        traceback.print_exc()  # Print traceback to console
        return JsonResponse({"error": "Internal server error"}, status=500)
