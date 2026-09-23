from types import SimpleNamespace

from gateway.storefront import storefront


def test_storefront_only_advertises_explicit_configured_routes():
    routes = {'semifly-27b': object(), 'unknown-secret-route': object()}
    result = storefront(SimpleNamespace(get=routes.get), [])
    assert len(result['categories']) == 5
    assert [m['id'] for m in result['models']] == ['semifly-27b']
    assert result['models'][0]['offers'] == []
    assert 'base_url' not in str(result)
    assert 'api_key' not in str(result)


def test_storefront_prices_are_original_product_records():
    product = {'model': 'semifly-27b', 'price_cents': 123, 'token_amount': 1000}
    result = storefront(SimpleNamespace(get=lambda name: object()), [product])
    assert result['models'][0]['offers'] == [product]
    assert storefront(SimpleNamespace(get=lambda name: None), [product])['models'] == []
