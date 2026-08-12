"""
app/shared/email_template.py
─────────────────────────────────────────────────────────────────────────────
Single source-of-truth for TWAM branded email HTML.

Usage
─────
from app.shared.email_template import twam_email

html = twam_email(
    title   = "Order Confirmed!",
    content = "<p>Your order #ORD1234 has been placed.</p>",
)

The logo is embedded as a Base64 data-URI so it renders in every email
client (Gmail, Outlook, Apple Mail) without relying on an external URL.

If you prefer an absolute URL pass logo_url:
    twam_email(..., logo_url="https://www.onlytwam.com/assets/Twam-Logo-White.png")

Template layout
───────────────
┌─────────────────────────────────────────┐
│  HEADER  2cm  — burgundy, logo centred  │
├─────────────────────────────────────────┤
│                                         │
│  CONTENT  — white card, variable height │
│                                         │
├─────────────────────────────────────────┤
│  FOOTER   — burgundy, address + support │
└─────────────────────────────────────────┘
"""

from __future__ import annotations
import base64, os
from pathlib import Path

# ── Brand colours ─────────────────────────────────────────────────────────────
_BURGUNDY    = "#6A012A"
_IVORY       = "#F6EEE1"
_IVORY_DARK  = "#EDE0CC"
_IVORY_MID   = "#F0E6D0"
_TEXT        = "#2C1018"
_MUTED       = "#9B7080"

# ── Logo — embedded as Base64 for maximum email client compatibility ──────────
#
# Email clients (especially Outlook, corporate firewalls, Gmail "images off")
# block external image URLs by default.  Embedding the logo as a data-URI
# means it always renders, no external request needed.
#
# How the logo is resolved (in order):
#   1. Explicit logo_b64 kwarg passed by the caller
#   2. File at TWAM_LOGO_PATH env var
#   3. File at <this file's dir>/../../static/Twam-Logo-White.png
#   4. File at /app/static/Twam-Logo-White.png  (Docker default)
#   5. Falls back to an absolute HTTPS URL (works if images are enabled)

_LOGO_URL_FALLBACK = "https://www.onlytwam.com/assets/Twam-Logo-White.png"

def _find_logo_file() -> str | None:
    candidates = [
        os.environ.get("TWAM_LOGO_PATH", ""),
        str(Path(__file__).parent.parent.parent / "static" / "Twam-Logo-White.png"),
        "/app/static/Twam-Logo-White.png",
        str(Path(__file__).parent.parent / "static" / "Twam-Logo-White.png"),
    ]
    for p in candidates:
        if p and Path(p).is_file():
            return p
    return None


def _logo_src(logo_url: str | None = None, logo_b64: str | None = None) -> str:
    """Return the src attribute value for the logo <img>."""
    # 1. Explicit base64 blob passed by caller
    if logo_b64:
        return f"data:image/png;base64,{logo_b64}"
    # 2. Explicit URL passed by caller
    if logo_url:
        return logo_url
    # 3. Try to load from disk and embed
    path = _find_logo_file()
    if path:
        try:
            b64 = base64.b64encode(Path(path).read_bytes()).decode()
            return f"data:image/png;base64,{b64}"
        except Exception:
            pass
    # 4. Fall back to hosted URL
    return _LOGO_URL_FALLBACK


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def twam_email(
    *,
    content   : str,
    title     : str  = "",
    logo_url  : str | None = None,
    logo_b64  : str | None = None,
    website   : str  = "www.onlytwam.com",
    support   : str  = "www.onlytwam.com/contact-us",
) -> str:
    """
    Wrap `content` in the standard TWAM email shell.

    Parameters
    ──────────
    content   HTML snippet for the body (everything between header and footer).
    title     Optional pre-header / preview text (shown in inbox before open).
    logo_url  Override the logo src with a URL (skips file/base64 lookup).
    logo_b64  Override with a raw base64 PNG string (no data-URI prefix needed).
    website   Footer website text  (default: www.onlytwam.com)
    support   Footer support link text  (default: www.onlytwam.com/contact-us)

    Returns
    ───────
    A complete, self-contained HTML email string ready to pass to send_email().
    """
    logo_src = _logo_src(logo_url, logo_b64)
    preheader = f'<span style="display:none;max-height:0;overflow:hidden;">{title}</span>' if title else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta http-equiv="X-UA-Compatible" content="IE=edge"/>
  <title>{title}</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>
  </o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    body {{ margin:0;padding:0;background:{_IVORY};font-family:Georgia,'Times New Roman',serif; }}
    img  {{ border:0;display:block; }}
    a    {{ color:{_BURGUNDY}; }}
    @media only screen and (max-width:600px) {{
      .email-card {{ width:100% !important; }}
      .body-pad   {{ padding:24px 20px !important; }}
    }}
  </style>
</head>
<body>
{preheader}

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{_IVORY};padding:32px 16px;">
  <tr>
    <td align="center">

      <!-- Email card — max 640px -->
      <table class="email-card" width="640" cellpadding="0" cellspacing="0" border="0"
             style="max-width:640px;width:100%;background:#ffffff;
                    border-radius:3px;overflow:hidden;
                    box-shadow:0 2px 0 {_BURGUNDY},0 8px 32px rgba(106,1,42,0.12);">

        <!-- ══ HEADER — 2cm burgundy band, logo centred ══ -->
        <tr>
          <td style="background:{_BURGUNDY};padding:0;height:76px;" align="center">
            <!--[if mso]>
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td width="100%" align="center" valign="middle" style="height:76px;background:{_BURGUNDY};">
            <![endif]-->
            <img src="{logo_src}"
                 alt="TWAM — Comfort Meets Confidence"
                 height="38"
                 style="height:38px;max-height:38px;display:inline-block;"/>
            <!--[if mso]></td></tr></table><![endif]-->
          </td>
        </tr>

        <!-- Accent line -->
        <tr>
          <td style="height:3px;background:{_IVORY_DARK};font-size:0;line-height:0;">&nbsp;</td>
        </tr>

        <!-- ══ CONTENT ══ -->
        <tr>
          <td class="body-pad"
              style="background:#ffffff;padding:36px 40px 32px;
                     font-family:Georgia,'Times New Roman',serif;
                     font-size:14px;color:{_TEXT};line-height:1.65;">
            {content}
          </td>
        </tr>

        <!-- Accent line above footer -->
        <tr>
          <td style="height:2px;background:{_IVORY_DARK};font-size:0;line-height:0;">&nbsp;</td>
        </tr>

        <!-- ══ FOOTER — 0.5cm burgundy band ══ -->
        <tr>
          <td style="background:{_BURGUNDY};padding:10px 32px;" align="center">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td align="center"
                    style="font-family:Georgia,serif;font-size:8pt;
                           color:rgba(246,238,225,0.85);
                           letter-spacing:0.14em;text-transform:uppercase;
                           white-space:nowrap;">
                  {website}
                  &nbsp;&nbsp;<span style="color:rgba(246,238,225,0.30);">|</span>&nbsp;&nbsp;
                  Customer Support:&nbsp;{support}
                </td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
      <!-- /email card -->

    </td>
  </tr>
</table>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# CONTENT BLOCK HELPERS
# Call these to build the `content` string you pass to twam_email().
# ─────────────────────────────────────────────────────────────────────────────

def greeting(customer_name: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:15px;">Hi <strong>{customer_name}</strong>,</p>'


def info_box(rows: list[tuple[str, str]], accent: str = _BURGUNDY) -> str:
    """Render a key-value info box with a left accent border."""
    inner = "".join(
        f'<tr>'
        f'<td style="padding:4px 0;font-size:12px;color:{_MUTED};'
        f'letter-spacing:0.08em;text-transform:uppercase;white-space:nowrap;'
        f'padding-right:20px;">{k}</td>'
        f'<td style="padding:4px 0;font-size:13px;color:{_TEXT};font-weight:600;">{v}</td>'
        f'</tr>'
        for k, v in rows
    )
    return f"""
    <table cellpadding="0" cellspacing="0" border="0"
           style="margin:20px 0;padding:16px 20px;
                  background:{_IVORY_MID};
                  border-left:5px solid {accent};
                  border-radius:2px;width:100%;">
      {inner}
    </table>"""


def items_table(items: list[dict]) -> str:
    """
    Render a product items table.
    Each item: {name, quantity, price, size?, color?, cup_size?}
    """
    def _fmt(v: float) -> str:
        return f"₹{v:,.0f}"

    rows = ""
    for item in items:
        details = []
        if item.get("size"):     details.append(f"Size: {item['size']}")
        if item.get("color"):    details.append(f"Color: {item['color']}")
        if item.get("cup_size"): details.append(f"Cup: {item['cup_size']}")
        if item.get("stock_id"): details.append(f"Stock: #{item['stock_id']}")
        det = (
            f'<br/><span style="font-size:11px;color:{_MUTED};">{" | ".join(details)}</span>'
            if details else ""
        )
        total = (item.get("price") or 0) * (item.get("quantity") or 1)
        rows += f"""
        <tr>
          <td style="padding:12px 14px;border-bottom:1px solid {_IVORY_MID};
                     font-size:13px;color:{_TEXT};">{item.get('name','')}{det}</td>
          <td style="padding:12px 14px;border-bottom:1px solid {_IVORY_MID};
                     text-align:center;font-size:13px;color:{_MUTED};">{item.get('quantity',1)}</td>
          <td style="padding:12px 14px;border-bottom:1px solid {_IVORY_MID};
                     text-align:right;font-size:13px;color:{_MUTED};">{_fmt(item.get('price',0))}</td>
          <td style="padding:12px 14px;border-bottom:1px solid {_IVORY_MID};
                     text-align:right;font-size:13px;font-weight:600;color:{_TEXT};">{_fmt(total)}</td>
        </tr>"""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="border-collapse:collapse;margin:20px 0;">
      <thead>
        <tr style="background:{_BURGUNDY};">
          <th style="padding:9px 14px;text-align:left;font-size:8pt;
                     letter-spacing:0.22em;text-transform:uppercase;
                     color:rgba(246,238,225,0.75);font-weight:normal;">Product</th>
          <th style="padding:9px 14px;text-align:center;font-size:8pt;
                     letter-spacing:0.22em;text-transform:uppercase;
                     color:rgba(246,238,225,0.75);font-weight:normal;">Qty</th>
          <th style="padding:9px 14px;text-align:right;font-size:8pt;
                     letter-spacing:0.22em;text-transform:uppercase;
                     color:rgba(246,238,225,0.75);font-weight:normal;">Price</th>
          <th style="padding:9px 14px;text-align:right;font-size:8pt;
                     letter-spacing:0.22em;text-transform:uppercase;
                     color:rgba(246,238,225,0.75);font-weight:normal;">Total</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def totals_block(subtotal: float, shipping: float, total: float,
                 coupon: float = 0) -> str:
    """Render the subtotal / shipping / coupon / grand total block."""
    def _fmt(v: float) -> str: return f"₹{v:,.0f}"

    coupon_row = ""
    if coupon > 0:
        coupon_row = f"""
        <tr>
          <td style="padding:5px 0;font-size:11px;color:{_MUTED};
                     letter-spacing:0.10em;text-transform:uppercase;">Coupon Discount</td>
          <td style="padding:5px 0;font-size:12px;color:#2E8B57;
                     font-weight:600;text-align:right;">−{_fmt(coupon)}</td>
        </tr>"""

    ship_str = "FREE" if shipping == 0 else _fmt(shipping)

    return f"""
    <table align="right" cellpadding="0" cellspacing="0" border="0"
           style="min-width:240px;border-collapse:collapse;margin-top:8px;">
      <tr>
        <td style="padding:5px 0;font-size:11px;color:{_MUTED};
                   letter-spacing:0.10em;text-transform:uppercase;
                   border-bottom:1px solid {_IVORY_MID};">Subtotal</td>
        <td style="padding:5px 0;font-size:12px;color:{_TEXT};text-align:right;
                   border-bottom:1px solid {_IVORY_MID};">{_fmt(subtotal)}</td>
      </tr>
      <tr>
        <td style="padding:5px 0;font-size:11px;color:{_MUTED};
                   letter-spacing:0.10em;text-transform:uppercase;
                   border-bottom:1px solid {_IVORY_MID};">Shipping</td>
        <td style="padding:5px 0;font-size:12px;color:{_TEXT};text-align:right;
                   border-bottom:1px solid {_IVORY_MID};">{ship_str}</td>
      </tr>
      {coupon_row}
      <tr>
        <td colspan="2" style="padding:4px 0;"></td>
      </tr>
      <tr>
        <td colspan="2" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:linear-gradient(135deg,{_IVORY} 0%,#fff 100%);
                        border:1.5px solid {_IVORY_DARK};
                        border-left:5px solid {_BURGUNDY};
                        border-radius:2px;">
            <tr>
              <td style="padding:12px 16px;font-size:10px;letter-spacing:0.26em;
                         text-transform:uppercase;color:{_MUTED};">Total</td>
              <td style="padding:12px 16px;font-size:20px;font-weight:700;
                         color:{_BURGUNDY};text-align:right;letter-spacing:0.04em;">{_fmt(total)}</td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
    <div style="clear:both;"></div>"""


def cta_button(label: str, url: str) -> str:
    return f"""
    <p style="text-align:center;margin:24px 0;">
      <a href="{url}"
         style="display:inline-block;padding:12px 32px;
                background:{_BURGUNDY};color:{_IVORY};
                text-decoration:none;border-radius:2px;
                font-size:12px;letter-spacing:0.16em;
                text-transform:uppercase;font-weight:600;">{label}</a>
    </p>"""


def section_divider() -> str:
    return f'<hr style="border:none;border-top:1.5px solid {_IVORY_DARK};margin:24px 0;"/>'