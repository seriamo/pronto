# iOS Shortcut — Pronto Location Updater

## Overview
An iOS Shortcut that runs at 8am on Tue/Wed/Thu, checks your GPS distance to the office, and updates the desk display accordingly.

## Authentication
All requests go through Cloudflare Access. The iOS Shortcut uses a **Service Token** — include these headers on every request:

```
CF-Access-Client-Id: <your-cf-access-client-id>
CF-Access-Client-Secret: <your-service-token-secret>
```

The app grants owner access only to service tokens you list. Add the token's **Client ID** to `.env` on the Pi, then restart the service:

```
OWNER_SERVICE_TOKENS=<your-cf-access-client-id>
```

Send the request body as JSON (`Content-Type: application/json`). Other content types are rejected.

## API Endpoints
- **Base URL**: `https://desk.example.com` (your tunnel hostname)
- `GET /api/location` — current location status
- `POST /api/location/set` — set override: `{"location": "office|remote", "date": "YYYY-MM-DD"}`
- `GET /api/ooo` — current OOO override status
- `POST /api/ooo/set` — set OOO: `{"return_date": "YYYY-MM-DD"}` or clear: `{"action": "clear"}`
- `POST /api/refresh` — force refresh calendar + reload display

## Create the Shortcut

### Step-by-step in iOS Shortcuts app:

1. **Open Shortcuts** → tap **+** → name it **"Update Desk Display"**

2. **Get Current Location**
   - Add action: **Get Current Location**

3. **Get Distance to Office**
   - Add action: **Get Distance**
   - From: `Current Location`
   - To: `99.99999999, -111.11111111111` (type the coordinates as the destination)
   - Units: **Miles**

4. **If distance < 2 miles** → auto-set office
   - Add action: **If**
   - Input: `Distance`
   - Condition: **is less than**
   - Value: `2`

5. **Inside the IF (near office):**
   - Add action: **Get Contents of URL**
   - URL: `https://desk.example.com/api/location/set`
   - Method: **POST**
   - Headers:
     - `Content-Type` = `application/json`
     - `CF-Access-Client-Id` = `<your-cf-access-client-id>`
     - `CF-Access-Client-Secret` = `<your-service-token-secret>`
   - Request Body: **JSON** → `location` = `office`
   - Add action: **Show Notification** → "📍 Desk display set to: Office"

6. **Otherwise (far from office):**
   - Add action: **Choose from Menu**
   - Prompt: "You're not near the office. Heading in today?"
   - Options: `Yes, heading in` / `No, working remote`
   
   **If "Yes, heading in":**
   - Add action: **Get Contents of URL**
   - URL: `https://desk.example.com/api/location/set`
   - Method: **POST**
   - Headers: (same 3 headers as above)
   - Request Body: **JSON** → `location` = `office`
   - Add action: **Show Notification** → "📍 Desk display set to: Office"
   
   **If "No, working remote":**
   - Add action: **Get Contents of URL**
   - URL: `https://desk.example.com/api/location/set`
   - Method: **POST**
   - Headers: (same 3 headers as above)
   - Request Body: **JSON** → `location` = `remote`
   - Add action: **Show Notification** → "📍 Desk display set to: Remote"

7. **End If**

### Set Up the Automation

1. Go to **Shortcuts** → **Automation** tab
2. Tap **+** → **Time of Day**
3. Time: **8:00 AM**
4. Repeat: **Weekly** → select **Tuesday, Wednesday, Thursday**
5. Start Immediately: **ON** (so it doesn't require confirmation)
6. Action: **Run Shortcut** → select **"Update Desk Display"**

### Manual Toggle (optional)

Create two simple one-tap shortcuts for your Home Screen:

**"Set Office":**
1. Get Contents of URL → `https://desk.example.com/api/location/set`
2. POST, JSON body: `{"location": "office"}`
3. Headers: CF-Access-Client-Id + CF-Access-Client-Secret
4. Show Notification: "📍 Set to Office"

**"Set Remote":**
1. Get Contents of URL → `https://desk.example.com/api/location/set`
2. POST, JSON body: `{"location": "remote"}`
3. Headers: CF-Access-Client-Id + CF-Access-Client-Secret
4. Show Notification: "📍 Set to Remote"

Add both to your Home Screen for quick one-tap toggling.

### Set OOO Override

Create a shortcut to set yourself as OOO with a return date:

**"Set OOO":**
1. Add action: **Ask for Input**
   - Prompt: "When do you return?"
   - Input Type: **Date**
2. Add action: **Format Date**
   - Date: `Provided Input`
   - Format: **Custom** → `yyyy-MM-dd`
3. Add action: **Get Contents of URL**
   - URL: `https://desk.example.com/api/ooo/set`
   - Method: **POST**
   - Headers:
     - `Content-Type` = `application/json`
     - `CF-Access-Client-Id` = `<your-cf-access-client-id>`
     - `CF-Access-Client-Secret` = `<your-service-token-secret>`
   - Request Body: **JSON** → `return_date` = `Formatted Date`
4. Add action: **Show Notification** → "✈️ OOO set until [Formatted Date]"

**"Clear OOO":**
1. Add action: **Get Contents of URL**
   - URL: `https://desk.example.com/api/ooo/set`
   - Method: **POST**
   - Headers: (same 3 headers as above)
   - Request Body: **JSON** → `action` = `clear`
2. Add action: **Show Notification** → "✅ OOO cleared"

**Check OOO status:**
- `GET /api/ooo` — returns `{"active": true/false, "return_date": "...", "return_date_human": "..."}`

## Security
- All remote endpoints are protected by **Cloudflare Access** (Zero Trust). The app also verifies the Access JWT itself.
- Browser access is authenticated by email. Owner pages require your address in `OWNER_EMAILS`.
- The iOS Shortcut authenticates with a **Service Token**, which must be listed in `OWNER_SERVICE_TOKENS`. Rotate it if your phone is lost.
- The Pi itself reaches `localhost:5001` directly and doesn't go through Cloudflare.

## Troubleshooting

**iOS Shortcuts shows "Could not connect" but the request works:**
This happened with Flask's development server and HTTP keep-alive through Cloudflare. The app now runs on [waitress](https://docs.pylonsproject.org/projects/waitress/), which handles keep-alive correctly. Don't set `Connection` headers in Flask responses: waitress rejects hop-by-hop headers with a 500. If the problem comes back, check `journalctl -u desk-display` and the Cloudflare Access logs.
