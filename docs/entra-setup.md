# Discover Chat Bot — IT / Power BI admin setup request

**For:** Microsoft Entra administrator and Fabric / Power BI administrator
**From:** Discover Chat Bot project team
**Purpose:** Enable an internal AI chatbot, delivered as a Power BI custom visual, that answers questions over Power BI
semantic models **using each user's own Power BI permissions** (single sign-on). It doesn't use a shared service account.

---

## Summary of what we need

| # | Item | Who | Required for |
|---|---|---|---|
| 1 | Confirm a **verified custom domain** we can use (e.g. `chatbot-api.<company-domain>`) | Entra admin | App registration (Microsoft rejects `onmicrosoft.com`) |
| 2 | Create one **app registration** (details below) | Entra admin, or grant us the *Application Developer* role | Sign-in + backend identity |
| 3 | **Grant admin consent** for its delegated permissions | Cloud Application Administrator / Global Administrator | No per-user consent prompts |
| 4 | Enable **Power BI tenant settings** (list below), scoped to a pilot security group if preferred | Fabric / Power BI administrator | Visual SSO + querying |
| 5 | Name **2–3 test users** with different model access (one under row-level security, if used) and a test workspace with ≥ 3 semantic models | Power BI admin / workspace owner | Security testing |
| 6 | Tell us whether **Conditional Access** policies apply to Power BI (MFA, compliant device, location) | Entra admin | Token exchange behaviour |

---

## 1. Verified custom domain

Check: **Microsoft Entra admin center → Settings → Domain names**. We need a domain whose status is **Verified** and
which doesn't end in `onmicrosoft.com`. We will use a sub-address of it as the API identifier, e.g.
`https://chatbot-api.<company-domain>`. Only the identifier string is required at this stage. DNS for the actual backend
host comes later.

> Microsoft requirement for custom-visual SSO: the Application ID URI "must begin with `https://` and must not contain
> `onmicrosoft.com`", and must be a verified custom domain.

## 2. App registration

Microsoft Entra admin center → **App registrations → New registration**

| Field | Value |
|---|---|
| Name | `Discover-Chat-Bot-API` (suggested; use separate registrations for dev and prod) |
| Supported account types | **Accounts in any organizational directory (Multitenant)**. This is required by Microsoft for the Power BI visual SSO API. Our backend only accepts tokens from our own tenant ID. |
| Redirect URI | None |

Then:

**2a. Expose an API**
- Application ID URI: `https://chatbot-api.<company-domain>` (from step 1).
- Add a scope named **`discoverChatBot09E811F9CAF94C58AD6EEF5D7849A3F7_CV_ForPBI`**, with "Who can consent" set to
  *Admins and users*. This name is 57 characters and the portal form allows only 40, so set it in the
  **Manifest** (`api.oauth2PermissionScopes[].value`) as Microsoft's documentation describes.
- **Authorized client applications.** Add each of these Microsoft Power BI app IDs and tick the scope above:

| Power BI client | Application (client) ID |
|---|---|
| Power BI Service (web) | `871c010f-5e61-4fb1-83ac-98610a7e9110` |
| Power BI Desktop | `7f67af8a-fedc-4b08-8b4e-37c4d127b6cf` |
| Power BI Mobile | `c0d2a505-13b8-4ae0-aa9e-cddd5eab0b12` |

**2b. API permissions (all Delegated, so the backend acts as the signed-in user only)**

| API | Permission | Why |
|---|---|---|
| Microsoft Graph | `User.Read` | Required by Microsoft for visual SSO token issuance |
| Power BI Service | `Dataset.Read.All` | Read semantic models the user can already access |
| Power BI Service | `Item.Read.All` | Read Fabric items the user can already access (Fabric IQ MCP) |
| Power BI Service | `Item.Execute.All` | Run read-only queries the user is already allowed to run (Fabric IQ MCP) |
| Power BI Service | `Workspace.Read.All` | List workspaces the user can already access |

No *Application* permissions are requested. The backend can never act without a signed-in user, and it can never see
more than that user can see in Power BI. Row-level and object-level security continue to apply.

**2c. Certificates & secrets**
- Create a **certificate** (preferred) or client secret for the backend. It is used for the On-Behalf-Of token exchange.
- It will be stored in a key vault, never in the visual or in source code. Please share it through a secure channel
  only.

**2d. Grant admin consent** for the permissions in 2b.

**Please send back:** Directory (tenant) ID, Application (client) ID, Application ID URI. Send the credential separately,
through a secure channel.

## 3. Power BI / Fabric tenant settings

Admin portal: **Tenant settings**. These can be enabled for a specific security group (pilot users) rather than the
whole organization.

| Setting (exact name) | Section | Why |
|---|---|---|
| **AppSource Custom Visuals SSO** | Power BI visuals | Lets the chatbot visual get an Entra token for the signed-in user. **Off by default.** |
| **Allow visuals created using the Power BI SDK** | Power BI visuals | Lets developers load the visual in debug/test mode during development |
| **Add and use certified visuals only (block uncertified)** | Power BI visuals | Must be **off** for the pilot (or the visual added to Organizational visuals) |
| **Dataset Execute Queries REST API** | Integration settings | Fallback query path (requires the user to have Build permission on the model) |
| **Users can use the Power BI Model Context Protocol server endpoint (preview)** | Integration settings | Microsoft's MCP query endpoint |

Primary query path: Microsoft's **Fabric IQ MCP server** (`https://fabriciq.svc.cloud.microsoft/v1/mcp/fabriciq`).
Microsoft documents it as available only when the tenant's **home region supports all Fabric workloads**. Please
confirm the tenant home region (**Help (?) → About Power BI → "Your data is stored in"**).

## 4. Security notes for review

- Every Power BI query runs with **the signed-in user's own delegated token** (OAuth 2.0 On-Behalf-Of). Power BI
  enforces workspace permissions, item permissions, row-level security and object-level security.
- The backend accepts tokens **only from our tenant ID**, even though the app registration is multitenant.
- Question text, model metadata and query results are sent to the configured LLM provider. The provider choice will be
  reviewed with you before production.
- Production distribution of the visual is planned through AppSource, which Microsoft requires for visual SSO.

## References

- Authentication API for custom visuals — https://learn.microsoft.com/en-us/power-bi/developer/visuals/authentication-api
- Entra app setup for visual SSO — https://learn.microsoft.com/en-us/power-bi/developer/visuals/entra-id-authentication
- Power BI visuals tenant settings — https://learn.microsoft.com/en-us/fabric/admin/service-admin-portal-power-bi-visuals
- Execute Queries REST API — https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/execute-queries
- Fabric IQ MCP server — https://learn.microsoft.com/en-us/fabric/iq/connectors/fabric-iq-mcp
