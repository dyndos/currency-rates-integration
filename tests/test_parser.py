from main import parse_xml

SAMPLE_XML = b"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="12.10.2026" name="Foreign Currency Market">
    <Valute ID="R01235">
        <NumCode>840</NumCode>
        <CharCode>USD</CharCode>
        <Nominal>1</Nominal>
        <Name>Доллар США</Name>
        <Value>92,5000</Value>
    </Valute>
</ValCurs>"""

def test_parse_xml_basic():
    result = parse_xml(SAMPLE_XML)
    assert len(result) == 1
    assert result[0]["currency_code"] == "USD"
    assert float(result[0]["rate"]) == 92.5
