# Worker Attendance System — UI Design

## Design Direction

**Concept:** A modern, industrial attendance system that feels like a real workplace product—not a generic AI/SaaS dashboard.

**Visual references:** Apple-level restraint, Linear-style information hierarchy, modern warehouse hardware.

### Principles

- Minimal, confident, functional
- Attendance-first; no fleet-management features
- Large touch targets on the kiosk
- Very little cognitive load for workers
- Dense but organized information for admins
- No excessive gradients, floating cards, glassmorphism, or decorative AI visuals
- Status is communicated primarily through typography, spacing, and restrained color

### Core palette

| Token | Value | Usage |
|---|---|---|
| Ink | `#111318` | Primary text / dark surfaces |
| Paper | `#F7F7F5` | Main background |
| Muted | `#747983` | Secondary text |
| Line | `#E3E5E8` | Borders / dividers |
| Blue | `#4169E1` | Primary interaction |
| Green | `#1F9D68` | Successful check-in |
| Amber | `#C98A18` | Late / attention |
| Red | `#D94B4B` | Failed / missing / error |

### Typography

- **Primary:** Inter, Geist, or SF Pro
- Headings: 600 weight
- Body: 400–500
- Numbers: 600–700
- Avoid overly large marketing-style typography inside the admin product

---

# 01 — Worker Check-in Kiosk

## Purpose

The kiosk should answer one question immediately:

> **"How do I check in?"**

The worker should be able to walk up, look at the camera, and finish in roughly 2–3 seconds.

## Main Screen

```text
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  LOGO / COMPANY                         08:42 AM             │
│                                         MON, AUG 25          │
│                                                              │
│                                                              │
│                     Good morning.                            │
│                  Check in for your shift                     │
│                                                              │
│                   ╭─────────────────╮                        │
│                   │                 │                        │
│                   │     CAMERA      │                        │
│                   │                 │                        │
│                   │   FACE FRAME    │                        │
│                   │                 │                        │
│                   ╰─────────────────╯                        │
│                                                              │
│                  Look at the camera                           │
│             Keep your face inside the frame                   │
│                                                              │
│                                                              │
│                       ───── OR ─────                          │
│                                                              │
│             ┌─────────────────────────────┐                  │
│             │  ▣  Use Employee ID         │                  │
│             │     Tap your ID card        │              ›   │
│             └─────────────────────────────┘                  │
│                                                              │
│                                                              │
│  Privacy first · Secure attendance       Need help?          │
└──────────────────────────────────────────────────────────────┘
```

## Important UI choices

### Camera area

Do **not** use a giant futuristic HUD.

Use a simple circular or softly rounded camera frame with a subtle animated perimeter.

States:

1. **Idle**
   - Neutral outline
   - "Look at the camera"
2. **Detecting**
   - Very subtle blue progress motion
   - "Hold still"
3. **Recognized**
   - Green confirmation
   - Employee's first name
4. **Rejected**
   - Red/amber state
   - Clear reason without exposing sensitive recognition data

### Recognition success

```text
┌─────────────────────────────────────┐
│                                     │
│             ✓                       │
│                                     │
│       You're checked in, Alex       │
│                                     │
│             08:42 AM                │
│                                     │
│       Morning shift · Warehouse     │
│                                     │
│                                     │
│       Have a good shift.             │
│                                     │
└─────────────────────────────────────┘
```

Success should remain on screen for ~2 seconds and then automatically return to the idle state.

### Failed recognition

```text
┌─────────────────────────────────────┐
│                                     │
│             !                       │
│                                     │
│       We couldn't verify you        │
│                                     │
│       Try again or use your         │
│       Employee ID                   │
│                                     │
│       [ Try again ]                 │
│                                     │
└─────────────────────────────────────┘
```

Never expose technical details such as confidence scores, model errors, embeddings, or internal verification logic to workers.

## Kiosk footer

Keep it extremely small:

**Secure attendance · Privacy protected**

Optional:

**Need help? Contact your supervisor**

---

# 02 — Admin Attendance Dashboard

## Purpose

The admin dashboard is **not a fleet dashboard**.

It exists exclusively to answer:

- Who is working?
- Who checked in?
- Who is late?
- Who is absent?
- When did someone check in?
- Which workers have unusual attendance?
- Can I review the attendance history/audit trail?

---

## Dashboard layout

```text
┌──────────────┬────────────────────────────────────────────────────┐
│              │                                                    │
│  COMPANY     │  Attendance                         Aug 25, 2026  │
│              │                                                    │
│  Overview    │  Good morning, Rahim                              │
│  Attendance  │  Here's today's workforce status.                 │
│  Workers     │                                                    │
│  Shifts      │                                                    │
│  Reports     │                                                    │
│  Audit Log   │                                                    │
│              │                                                    │
│              │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐      │
│              │  │  142   │ │  124   │ │   12   │ │    6   │      │
│              │  │Present │ │On time │ │  Late  │ │Absent  │      │
│              │  └────────┘ └────────┘ └────────┘ └────────┘      │
│              │                                                    │
│              │  Attendance today              Live activity      │
│              │  ┌──────────────────────────┐  ┌────────────────┐ │
│              │  │                          │  │ Alex       8:39│ │
│              │  │       ╱╲                 │  │ Samir      8:37│ │
│              │  │     ╱    ╲      ╱╲       │  │ David      8:34│ │
│              │  │   ╱        ╲  ╱    ╲     │  │ Maria      8:31│ │
│              │  │ ╱            ╲╱      ╲    │  │ Carlos     8:28│ │
│              │  └──────────────────────────┘  └────────────────┘ │
│              │                                                    │
│              │  Today's attendance                               │
│              │  ┌──────────────────────────────────────────────┐ │
│              │  │ Worker       Shift      Status      Check-in │ │
│              │  │─────────────────────────────────────────────│ │
│              │  │ Alex Johnson Morning    Present      8:39 AM │ │
│              │  │ Samir Karimov Morning    Present      8:37 AM │ │
│              │  │ David Miller  Morning    Late        8:34 AM │ │
│              │  │ Maria Chen    Morning    Present      8:31 AM │ │
│              │  │ Carlos Lopez  Morning    Missing         —    │ │
│              │  └──────────────────────────────────────────────┘ │
│              │                                                    │
└──────────────┴────────────────────────────────────────────────────┘
```

---

## Top-level navigation

Keep navigation intentionally small:

- **Overview**
- **Attendance**
- **Workers**
- **Shifts**
- **Reports**
- **Audit Log**
- **Settings**

Do not include:

- Fleet
- Trucks
- Routes
- Deliveries
- GPS
- Maintenance

Those concepts distract from the actual product.

---

# Attendance Overview

The first screen should prioritize **today**.

### KPI row

Four primary metrics:

**Present**

Total workers who have checked in.

**On time**

Workers who checked in before their shift's late threshold.

**Late**

Workers who checked in after their expected start time.

**Absent**

Workers expected to work who have no valid check-in.

Keep KPI cards simple. Avoid circular progress indicators everywhere.

---

## Attendance chart

Use one useful visualization rather than a dashboard full of charts.

### Recommended

**Hourly check-ins**

```text
Check-ins
30 ┤                         ╭──╮
25 ┤                    ╭────╯  ╰╮
20 ┤               ╭────╯        ╰╮
15 ┤          ╭────╯               ╰
10 ┤     ╭────╯
 5 ┤ ╭───╯
 0 ┼─────────────────────────────────
    6   7   8   9   10  11  12  1 PM
```

This immediately tells an admin when workers actually arrived.

---

# Live Activity

A compact right-side feed:

```text
LIVE

● Alex Johnson
  Checked in · 8:39 AM

● Samir Karimov
  Checked in · 8:37 AM

● David Miller
  Checked in · 8:34 AM

● Maria Chen
  Checked in · 8:31 AM
```

Use a subtle live indicator rather than flashy animation.

---

# Attendance Table

This is the most important admin component.

Columns:

| Worker | ID | Shift | Status | Check-in | Method | Confidence |
|---|---|---|---|---|---|---|
| Alex Johnson | 4827 | Morning | Present | 8:39 AM | Face | 99.6% |
| Samir Karimov | 5612 | Morning | Present | 8:37 AM | Face | 98.9% |
| David Miller | 4471 | Morning | Late | 8:34 AM | ID | — |
| Maria Chen | 3912 | Morning | Present | 8:31 AM | Face | 99.2% |
| Carlos Lopez | 5099 | Morning | Absent | — | — | — |

### Table interactions

- Search worker
- Filter by date
- Filter by shift
- Filter by status
- Filter by check-in method
- Export CSV
- Click a worker to open attendance history

---

# Worker Profile

Clicking a worker should open a focused attendance profile.

```text
← Workers

Alex Johnson
Employee ID 4827
Morning Shift

┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ 98.7%         │  │ 21             │  │ 3             │
│ On-time rate  │  │ Days present  │  │ Late arrivals │
└───────────────┘  └───────────────┘  └───────────────┘

Attendance history

Aug 25     Present       8:39 AM
Aug 24     Present       8:36 AM
Aug 23     Late          8:48 AM
Aug 22     Present       8:37 AM
Aug 21     Present       8:35 AM
```

The profile should feel like an employee record, not a social profile.

---

# Audit Log

Because the system uses facial verification, the audit interface should be especially clear.

```text
Audit Log

Timestamp       Worker          Event              Method
────────────────────────────────────────────────────────────
08:39:12        Alex Johnson    Check-in           Face
08:37:44        Samir Karimov   Check-in           Face
08:34:08        David Miller    Check-in           Employee ID
08:30:21        —               Verification fail  Face
08:29:54        —               Verification fail  Face
```

Each event can open a detail drawer containing:

- Timestamp
- Worker
- Event type
- Kiosk/location name
- Authentication method
- Verification result
- Audit event ID

Avoid showing raw biometric data.

---

# Responsive behavior

## Kiosk

Designed specifically for:

- 1080×1920 portrait
- 1200×1920 portrait
- 1920×1080 landscape

Portrait should be the primary design.

## Admin

Desktop-first:

- 1440px ideal
- 1280px minimum
- Tablet can use a collapsed sidebar

---

# Component style

### Buttons

Primary:

```text
┌─────────────────────────┐
│       Try again          │
└─────────────────────────┘
```

Use medium-radius buttons rather than giant pills.

### Cards

Cards should have:

- 1px subtle border
- 12–16px radius
- Minimal/no shadow
- Clear hierarchy
- Consistent internal padding

### Status labels

Use restrained labels:

`Present` → green

`Late` → amber

`Absent` → red

`Pending` → neutral

Avoid giant colored badges.

---

# Final product feel

The product should feel like **attendance infrastructure for a serious logistics company**.

Not:

> "AI-powered workforce intelligence platform"

Instead:

> **Fast. Clear. Accountable.**

A worker sees almost nothing except what they need to check in.

An administrator gets a calm operational view of **people, shifts, attendance, exceptions, and audit history**.

The visual identity should come from the typography, spacing, iconography, and interaction details—not from decorative gradients or AI-generated dashboard clutter.
