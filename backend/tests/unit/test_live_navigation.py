import pytest
from src.app.agent.brain.text_utils import append_live_navigation

def test_navigation_to_home_page():
    ui_actions = []
    store_context = {"url": "https://speako-demo.com"}
    append_live_navigation(
        ui_actions,
        store_context=store_context,
        query="go to the home page",
        platform="woocommerce",
        current_url="https://speako-demo.com/cart"
    )
    assert len(ui_actions) == 1
    assert ui_actions[0]["type"] == "redirect"
    assert ui_actions[0]["payload"]["url"] == "https://speako-demo.com"
    assert ui_actions[0]["payload"]["reason"] == "home"

def test_navigation_to_cart_page():
    ui_actions = []
    store_context = {"url": "https://speako-demo.com", "cart_url": "https://speako-demo.com/cart"}
    append_live_navigation(
        ui_actions,
        store_context=store_context,
        query="show my cart",
        platform="woocommerce",
        current_url="https://speako-demo.com"
    )
    assert len(ui_actions) == 1
    assert ui_actions[0]["type"] == "redirect"
    assert ui_actions[0]["payload"]["url"] == "https://speako-demo.com/cart"
    assert ui_actions[0]["payload"]["reason"] == "cart"

def test_navigation_to_checkout_page():
    ui_actions = []
    store_context = {"url": "https://speako-demo.com", "checkout_url": "https://speako-demo.com/checkout"}
    append_live_navigation(
        ui_actions,
        store_context=store_context,
        query="take me to checkout",
        platform="woocommerce",
        current_url="https://speako-demo.com/cart"
    )
    assert len(ui_actions) == 1
    assert ui_actions[0]["type"] == "redirect"
    assert ui_actions[0]["payload"]["url"] == "https://speako-demo.com/checkout"
    assert ui_actions[0]["payload"]["reason"] == "checkout"

def test_navigation_to_first_product_from_history():
    ui_actions = []
    store_context = {"url": "https://speako-demo.com"}
    last_products = [
        {"name": "Black Running Shoes", "permalink": "https://speako-demo.com/product/black-running-shoes"},
        {"name": "White Sneakers", "permalink": "https://speako-demo.com/product/white-sneakers"}
    ]
    append_live_navigation(
        ui_actions,
        store_context=store_context,
        query="go to the first product",
        platform="woocommerce",
        current_url="https://speako-demo.com",
        last_products=last_products
    )
    assert len(ui_actions) == 1
    assert ui_actions[0]["type"] == "redirect"
    assert ui_actions[0]["payload"]["url"] == "https://speako-demo.com/product/black-running-shoes"
    assert ui_actions[0]["payload"]["reason"] == "product"

def test_navigation_to_second_product_from_history():
    ui_actions = []
    store_context = {"url": "https://speako-demo.com"}
    last_products = [
        {"name": "Black Running Shoes", "permalink": "https://speako-demo.com/product/black-running-shoes"},
        {"name": "White Sneakers", "permalink": "https://speako-demo.com/product/white-sneakers"}
    ]
    append_live_navigation(
        ui_actions,
        store_context=store_context,
        query="show the second one",
        platform="woocommerce",
        current_url="https://speako-demo.com",
        last_products=last_products
    )
    assert len(ui_actions) == 1
    assert ui_actions[0]["type"] == "redirect"
    assert ui_actions[0]["payload"]["url"] == "https://speako-demo.com/product/white-sneakers"
    assert ui_actions[0]["payload"]["reason"] == "product"
