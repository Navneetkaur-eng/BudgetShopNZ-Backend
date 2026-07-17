from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from io import BytesIO
import datetime
import csv
import os
import re

app = Flask(__name__)
CORS(app)

# ═══════════════════════════════════════
# NUTRITION: PER-100g/100ml BASELINES BY CATEGORY
# (used together with each item's real package size,
#  parsed from its name, so two items in the same
#  category no longer get identical nutrition totals)
# ═══════════════════════════════════════
PER_100_NUTRITION = {
    "Dairy":     {"calories": 65,  "protein": 3.4, "carbs": 5,  "fat": 3.6, "fibre": 0},
    "Eggs":      {"calories": 155, "protein": 13,  "carbs": 1,  "fat": 11,  "fibre": 0},
    "Bread":     {"calories": 250, "protein": 9,   "carbs": 47, "fat": 3,   "fibre": 4},
    "Meat":      {"calories": 200, "protein": 25,  "carbs": 0,  "fat": 12,  "fibre": 0},
    "Seafood":   {"calories": 130, "protein": 22,  "carbs": 0,  "fat": 5,   "fibre": 0},
    "Vegetables":{"calories": 35,  "protein": 2,   "carbs": 7,  "fat": 0.2, "fibre": 3},
    "Fruit":     {"calories": 55,  "protein": 0.7, "carbs": 14, "fat": 0.2, "fibre": 2.5},
    "Pantry":    {"calories": 350, "protein": 8,   "carbs": 65, "fat": 4,   "fibre": 3},
    "Breakfast": {"calories": 370, "protein": 9,   "carbs": 68, "fat": 5,   "fibre": 8},
    "Beverages": {"calories": 42,  "protein": 0.3, "carbs": 10, "fat": 0,   "fibre": 0},
    "Snacks":    {"calories": 500, "protein": 6,   "carbs": 55, "fat": 28,  "fibre": 3},
    "Condiments":{"calories": 20,  "protein": 0.5, "carbs": 3,  "fat": 0.5, "fibre": 0},
}

# Keywords that should be treated as low-calorie condiments/seasonings
# even though their CSV category is "Pantry" — otherwise something
# like a 1kg bag of salt gets treated as 1kg of flour/rice calories
CONDIMENT_KEYWORDS = ["salt", "pepper", "sauce", "soy", "mayo", "vinegar", "spice"]

def is_condiment(item_name):
    name = item_name.lower()
    return any(k in name for k in CONDIMENT_KEYWORDS)

# Fallback package weight (grams) when an item's name has no
# parseable size — e.g. "Broccoli", "Avocado", "Garlic Bulb"
CATEGORY_DEFAULT_WEIGHT_G = {
    "Dairy": 500, "Eggs": 600, "Bread": 700, "Meat": 500, "Seafood": 300,
    "Vegetables": 150, "Fruit": 150, "Pantry": 400, "Breakfast": 500,
    "Beverages": 500, "Snacks": 200,
}

SIZE_PATTERN = re.compile(r'(\d+\.?\d*)\s*(kg|g|ml|l)\b', re.IGNORECASE)
PACK_PATTERN = re.compile(r'(\d+)\s*pk\b', re.IGNORECASE)
EGG_APPROX_WEIGHT_G = 55  # typical single egg weight

# ═══════════════════════════════════════
# NUTRITION GUIDELINES — WEEKLY, not daily.
# BudgetShop NZ optimises a WEEKLY shopping basket, so it must be
# validated against MoH NZ (2020) daily guidelines × 7, not a single
# day's target. Comparing a week's groceries to a one-day target
# would make every realistic basket look wildly "over guideline".
# ═══════════════════════════════════════
DAILY_GUIDELINES = {"calories": 2000, "protein": 50, "carbs": 250, "fat": 70, "fibre": 25}
WEEKLY_GUIDELINES = {k: v * 7 for k, v in DAILY_GUIDELINES.items()}


def parse_package_grams(item_name, category, unit_str=None):
    """Work out a realistic weight (in grams) for ONE unit of this
    product. Prefers the real 'unit' column from the CSV; falls back
    to parsing the item name, then to a category default."""

    def convert(value, unit):
        unit = unit.lower()
        if unit == "kg":
            return value * 1000
        if unit == "g":
            return value
        if unit == "l":
            return value * 1000
        if unit == "ml":
            return value
        return None

    # 1) Prefer the real CSV 'unit' column, e.g. "2L", "500g", "12pk"
    if unit_str:
        u = unit_str.strip().lower()
        pack_match = PACK_PATTERN.search(u)
        if pack_match and category == "Eggs":
            return int(pack_match.group(1)) * EGG_APPROX_WEIGHT_G
        size_match = SIZE_PATTERN.search(u)
        if size_match:
            grams = convert(float(size_match.group(1)), size_match.group(2))
            if grams:
                return grams

    # 2) Fall back to parsing the item name
    name = item_name.lower()
    pack_match = PACK_PATTERN.search(name)
    if pack_match and category == "Eggs":
        return int(pack_match.group(1)) * EGG_APPROX_WEIGHT_G
    size_match = SIZE_PATTERN.search(name)
    if size_match:
        grams = convert(float(size_match.group(1)), size_match.group(2))
        if grams:
            return grams

    # 3) Category default as last resort
    return CATEGORY_DEFAULT_WEIGHT_G.get(category, 400)


def build_item_nutrition(item_name, category, unit_str=None):
    effective_category = "Condiments" if is_condiment(item_name) else category
    baseline = PER_100_NUTRITION.get(effective_category, PER_100_NUTRITION["Pantry"])
    grams = parse_package_grams(item_name, effective_category, unit_str)
    factor = grams / 100.0
    return {k: round(v * factor, 1) for k, v in baseline.items()}

# ═══════════════════════════════════════
# LOAD GROCERY DATA FROM CSV
# ═══════════════════════════════════════
def load_grocery_data():
    grocery_data = {}
    nutrition_data = {}

    csv_path = os.path.join(os.path.dirname(__file__), 'nz_grocery_data.csv')

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['item_name'].strip()
                category = row['category'].strip()

                grocery_data[name] = {
                    "Pak'nSave": float(row['paknsave']),
                    "New World": float(row['newworld']),
                    "Woolworths": float(row['woolworths']),
                }

                nutrition_data[name] = build_item_nutrition(name, category, row.get('unit'))

        print(f"✅ Loaded {len(grocery_data)} items from CSV!")
    except Exception as e:
        print(f"❌ CSV Error: {e}")
        grocery_data = {
            "Anchor Full Cream Milk 2L": {"Pak'nSave": 2.99, "New World": 3.49, "Woolworths": 3.59},
            "Eggs Free Range 12pk":      {"Pak'nSave": 4.99, "New World": 4.49, "Woolworths": 5.19},
            "Broccoli":                  {"Pak'nSave": 1.99, "New World": 2.49, "Woolworths": 2.29},
        }
        nutrition_data = {
            "Anchor Full Cream Milk 2L": build_item_nutrition("Anchor Full Cream Milk 2L", "Dairy", "2L"),
            "Eggs Free Range 12pk":      build_item_nutrition("Eggs Free Range 12pk", "Eggs", "12pk"),
            "Broccoli":                  build_item_nutrition("Broccoli", "Vegetables", None),
        }

    return grocery_data, nutrition_data

grocery_data, nutrition_data = load_grocery_data()

# ═══════════════════════════════════════
# SMART ITEM MATCHING
# ═══════════════════════════════════════
def find_item(search_name):
    if search_name in grocery_data:
        return search_name
    for key in grocery_data:
        if key.lower() == search_name.lower():
            return key
    for key in grocery_data:
        if search_name.lower() in key.lower():
            return key
    for key in grocery_data:
        if key.lower() in search_name.lower():
            return key
    search_words = search_name.lower().split()
    for key in grocery_data:
        key_words = key.lower().split()
        if any(word in key_words for word in search_words if len(word) > 3):
            return key
    return None

# ═══════════════════════════════════════
# ROUTE 1 — HEALTH CHECK
# ═══════════════════════════════════════
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        "status": "BudgetShop NZ API running!",
        "version": "3.2.0",
        "total_items": len(grocery_data)
    })

# ═══════════════════════════════════════
# ROUTE 2 — GET ALL PRICES
# ═══════════════════════════════════════
@app.route('/api/prices', methods=['GET'])
def get_prices():
    return jsonify({"status": "success", "data": grocery_data})

# ═══════════════════════════════════════
# ROUTE 3 — SEARCH ITEMS
# ═══════════════════════════════════════
@app.route('/api/search', methods=['GET'])
def search_items():
    query = request.args.get('q', '').lower()
    if not query or len(query) < 2:
        popular = list(grocery_data.keys())[:20]
        return jsonify({"results": popular})
    results = [key for key in grocery_data if query in key.lower()]
    return jsonify({"results": results[:10]})

# ═══════════════════════════════════════
# ROUTE 4 — GET ALL ITEM NAMES
# ═══════════════════════════════════════
@app.route('/api/items', methods=['GET'])
def get_all_items():
    return jsonify({
        "status": "success",
        "items": list(grocery_data.keys()),
        "total": len(grocery_data)
    })

# ═══════════════════════════════════════
# ROUTE 5 — ML BUDGET OPTIMISATION
# ═══════════════════════════════════════
@app.route('/api/optimise', methods=['POST'])
def optimise():
    data = request.get_json()
    grocery_list = data.get('items', [])
    budget = float(data.get('budget', 150))
    dietary = data.get('dietary', 'No preference')

    item_quantities = {}
    unmatched_items = []

    for item_name in grocery_list:
        matched = find_item(item_name)
        if matched:
            if matched in item_quantities:
                item_quantities[matched] += 1
            else:
                item_quantities[matched] = 1
        else:
            unmatched_items.append(item_name)

    total_cost = 0
    store_plan = {}

    all_stores = ["Pak'nSave", "New World", "Woolworths"]
    single_store_totals = {store: 0 for store in all_stores}

    for item_name, qty in item_quantities.items():
        prices = grocery_data[item_name]
        cheapest_store = min(prices, key=prices.get)
        cheapest_price = prices[cheapest_store]
        total_item_price = round(cheapest_price * qty, 2)
        total_cost += total_item_price

        for store in all_stores:
            single_store_totals[store] += round(prices[store] * qty, 2)

        if cheapest_store not in store_plan:
            store_plan[cheapest_store] = []

        store_plan[cheapest_store].append({
            "name": item_name,
            "price": total_item_price,
            "qty": qty,
            "unit_price": cheapest_price,
            "all_prices": prices
        })

    single_store_totals = {k: round(v, 2) for k, v in single_store_totals.items()}
    best_single_store = min(single_store_totals.values()) if single_store_totals else total_cost
    savings = round(best_single_store - total_cost, 2)

    nutrition_total = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "fibre": 0}
    for item_name, qty in item_quantities.items():
        if item_name in nutrition_data:
            for key in nutrition_total:
                nutrition_total[key] += round(nutrition_data[item_name][key] * qty, 1)
    nutrition_total = {k: round(v, 1) for k, v in nutrition_total.items()}

    # Validated against WEEKLY MoH NZ (2020) guidelines — this is a
    # weekly shopping basket, not a single day's food
    meets = all(nutrition_total[k] >= WEEKLY_GUIDELINES[k] * 0.7 for k in WEEKLY_GUIDELINES)

    item_nutrition = {}
    for item_name, qty in item_quantities.items():
        if item_name in nutrition_data:
            nutr = nutrition_data[item_name]
            item_nutrition[item_name] = {
                "calories": round(nutr["calories"] * qty, 1),
                "protein": round(nutr["protein"] * qty, 1),
                "carbs": round(nutr["carbs"] * qty, 1),
                "fat": round(nutr["fat"] * qty, 1),
                "fibre": round(nutr["fibre"] * qty, 1),
            }

    return jsonify({
        "status": "success",
        "optimised_plan": store_plan,
        "total_cost": round(total_cost, 2),
        "savings": max(savings, 0),
        "single_store_totals": single_store_totals,
        "within_budget": total_cost <= budget,
        "budget": budget,
        "nutrition": nutrition_total,
        "nutrition_guidelines": WEEKLY_GUIDELINES,
        "item_nutrition": item_nutrition,
        "meets_moh_nz_2020": meets,
        "unmatched_items": unmatched_items,
        "items_found": len(item_quantities),
        "budget_remaining": round(budget - total_cost, 2)
    })

# ═══════════════════════════════════════
# ROUTE 6 — NUTRITION CHECK
# ═══════════════════════════════════════
@app.route('/api/nutrition', methods=['POST'])
def check_nutrition():
    data = request.get_json()
    grocery_list = data.get('items', [])

    total = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "fibre": 0}

    for item in grocery_list:
        matched = find_item(item)
        if matched and matched in nutrition_data:
            for key in total:
                total[key] += nutrition_data[matched][key]

    meets = all(total[k] >= WEEKLY_GUIDELINES[k] * 0.7 for k in WEEKLY_GUIDELINES)

    return jsonify({
        "status": "success",
        "nutrition": {k: round(v, 1) for k, v in total.items()},
        "guidelines": WEEKLY_GUIDELINES,
        "meets_moh_nz_2020": meets
    })

# ═══════════════════════════════════════
# ROUTE 7 — PDF REPORT
# ═══════════════════════════════════════
@app.route('/api/report', methods=['POST'])
def generate_report():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.colors import HexColor, white
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.units import inch

    data = request.get_json()
    store_plan = data.get('optimised_plan', {})
    total_cost = float(data.get('total_cost', 0))
    savings = float(data.get('savings', 0))
    budget = float(data.get('budget', 150))
    nutrition = data.get('nutrition', {})

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
        rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)

    styles = getSampleStyleSheet()
    story = []

    GREEN = HexColor('#2E7D32')
    LIGHT_GREEN = HexColor('#E8F5E9')
    DARK_GREEN = HexColor('#1B5E20')
    ORANGE = HexColor('#E65100')
    BLUE = HexColor('#1565C0')
    LIGHT_GREY = HexColor('#F5F5F5')

    header_data = [[
        Paragraph(f'<font color="white" size="18"><b>BudgetShop NZ</b></font><br/><font color="#A5D6A7" size="10">Weekly Shopping Plan · Auckland · {datetime.date.today().strftime("%d %B %Y")}</font>', styles['Normal']),
        Paragraph(f'<font color="white" size="16"><b>${total_cost:.2f} total</b></font><br/><font color="#A5D6A7" size="10">${savings:.2f} saved · Budget: ${budget:.2f}</font>', styles['Normal']),
    ]]
    header_table = Table(header_data, colWidths=[3.8*inch, 3.2*inch])
    header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), DARK_GREEN),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 14),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.2*inch))

    store_color_map = {
        "Pak'nSave": GREEN,
        "New World": ORANGE,
        "Woolworths": BLUE,
    }

    for store_name, items in store_plan.items():
        if not items:
            continue
        store_total = sum(item['price'] for item in items)
        store_color = store_color_map.get(store_name, GREEN)

        store_header = [[
            Paragraph(f'<font color="white" size="12"><b>{store_name}</b></font>', styles['Normal']),
            Paragraph(f'<font color="white" size="12"><b>${store_total:.2f}</b></font>', styles['Normal']),
        ]]
        store_table = Table(store_header, colWidths=[5.5*inch, 1.5*inch])
        store_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), store_color),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('PADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(store_table)

        for item in items:
            qty = item.get('qty', 1)
            unit_price = item.get('unit_price', item['price'])
            qty_text = f' x{qty}' if qty > 1 else ''
            unit_text = f' (${unit_price:.2f} each)' if qty > 1 else ''

            item_data = [[
                Paragraph(f'<font size="10" color="#333333">  {item["name"]}{qty_text}{unit_text}</font>', styles['Normal']),
                Paragraph(f'<font size="10" color="#2E7D32"><b>${item["price"]:.2f}</b></font>', styles['Normal']),
            ]]
            item_table = Table(item_data, colWidths=[5.5*inch, 1.5*inch])
            item_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GREEN),
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('PADDING', (0, 0), (-1, -1), 6),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, HexColor('#C8E6C9')),
            ]))
            story.append(item_table)
        story.append(Spacer(1, 0.12*inch))

    savings_data = [[
        Paragraph('<font size="12" color="#2E7D32"><b>Total saved vs single-store shopping</b></font>', styles['Normal']),
        Paragraph(f'<font size="14" color="#2E7D32"><b>${savings:.2f} saved!</b></font>', styles['Normal']),
    ]]
    savings_table = Table(savings_data, colWidths=[5*inch, 2*inch])
    savings_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GREEN),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 10),
        ('BOX', (0, 0), (-1, -1), 1, GREEN),
    ]))
    story.append(savings_table)
    story.append(Spacer(1, 0.15*inch))

    nutr_header = [[Paragraph('<font color="white" size="11"><b>Nutritional Summary — vs Weekly MoH NZ (2020) Guidelines</b></font>', styles['Normal'])]]
    nutr_header_table = Table(nutr_header, colWidths=[7*inch])
    nutr_header_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), GREEN),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(nutr_header_table)

    cal = round(nutrition.get('calories', 0))
    pro = round(nutrition.get('protein', 0))
    carbs = round(nutrition.get('carbs', 0))
    fibre = round(nutrition.get('fibre', 0))

    nutr_data = [[
        Paragraph(f'<font size="10"><b>{cal} kcal</b><br/>Calories</font>', styles['Normal']),
        Paragraph(f'<font size="10"><b>{pro}g</b><br/>Protein</font>', styles['Normal']),
        Paragraph(f'<font size="10"><b>{carbs}g</b><br/>Carbohydrates</font>', styles['Normal']),
        Paragraph(f'<font size="10"><b>{fibre}g</b><br/>Fibre</font>', styles['Normal']),
    ]]
    nutr_table = Table(nutr_data, colWidths=[1.75*inch, 1.75*inch, 1.75*inch, 1.75*inch])
    nutr_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GREY),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#E0E0E0')),
    ]))
    story.append(nutr_table)
    story.append(Spacer(1, 0.2*inch))

    story.append(Paragraph(
        '<font size="8" color="#999999">Generated by BudgetShop NZ — AI-Powered Grocery Budget Optimiser for New Zealand · github.com/Navneetkaur-eng/BudgetShopNZ</font>',
        styles['Normal']
    ))

    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'BudgetShopNZ_Report_{datetime.date.today()}.pdf',
        mimetype='application/pdf'
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)