<?php

if (!defined('ABSPATH')) {
    exit;
}

class WooAgent_API
{
    /** @var WooAgent_Auth */
    private $auth;

    public function __construct(WooAgent_Auth $auth)
    {
        $this->auth = $auth;
    }

    /**
     * Register REST hooks.
     */
    public function init()
    {
        add_action('rest_api_init', array($this, 'register_routes'));
    }

    /**
     * Register plugin API endpoints.
     */
    public function register_routes()
    {
        register_rest_route('wooagent/v1', '/chat', array(
            'methods' => WP_REST_Server::CREATABLE,
            'callback' => array($this, 'chat'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/session/(?P<session_id>[a-zA-Z0-9\-]+)', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'get_session'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/cart/add', array(
            'methods' => WP_REST_Server::CREATABLE,
            'callback' => array($this, 'cart_add'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/cart/remove', array(
            'methods' => WP_REST_Server::CREATABLE,
            'callback' => array($this, 'cart_remove'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/cart', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'cart_get'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/cart/update', array(
            'methods' => WP_REST_Server::CREATABLE,
            'callback' => array($this, 'cart_update'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/products/search', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'products_search'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/products/(?P<id>\d+)', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'product_detail'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/orders/(?P<email>[^/]+)', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'orders_by_email'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/products/(?P<id>\d+)/variations', array(
            'methods' => WP_REST_Server::READABLE,
            'callback' => array($this, 'product_variations'),
            'permission_callback' => array($this, 'authorize_request'),
        ));

        register_rest_route('wooagent/v1', '/products/(?P<id>\d+)/review', array(
            'methods' => WP_REST_Server::CREATABLE,
            'callback' => array($this, 'product_review'),
            'permission_callback' => array($this, 'authorize_request'),
        ));
    }

    /**
     * Shared request auth.
     *
     * @param WP_REST_Request $request
     * @return true|WP_Error
     */
    public function authorize_request($request)
    {
        $route = (string) $request->get_route();
        $method = strtoupper((string) $request->get_method());

        // Product catalog data is public; allow GET access without nonce/signature.
        if (
            $method === 'GET' &&
            (
                $route === '/wooagent/v1/products/search' ||
                (bool) preg_match('#^/wooagent/v1/products/\d+$#', $route)
            )
        ) {
            $session_id = sanitize_text_field((string) $request->get_param('session_id'));
            if (!$this->check_rate_limit($session_id, $route)) {
                return new WP_Error('wooagent_rate_limited', __('Rate limit exceeded.', 'wooagent'), array('status' => 429));
            }
            return true;
        }

        // Cart endpoints must work for guest shoppers from the storefront session.
        if (
            $route === '/wooagent/v1/cart' ||
            $route === '/wooagent/v1/cart/add' ||
            $route === '/wooagent/v1/cart/update' ||
            $route === '/wooagent/v1/cart/remove'
        ) {
            $session_id = sanitize_text_field((string) $request->get_param('session_id'));
            if ($session_id === '') {
                $session_id = sanitize_text_field((string) $request->get_header('x-wooagent-session'));
            }
            if (!$this->check_rate_limit($session_id, $route)) {
                return new WP_Error('wooagent_rate_limited', __('Rate limit exceeded.', 'wooagent'), array('status' => 429));
            }
            return true;
        }

        if (!$this->auth->validate_request($request)) {
            return new WP_Error('wooagent_unauthorized', __('Invalid signature or nonce.', 'wooagent'), array('status' => 401));
        }

        $session_id = sanitize_text_field((string) $request->get_param('session_id'));
        if ($session_id === '' && $route !== '/wooagent/v1/chat') {
            $session_id = sanitize_text_field((string) $request->get_header('x-wooagent-session'));
        }

        if (!$this->check_rate_limit($session_id, $route)) {
            return new WP_Error('wooagent_rate_limited', __('Rate limit exceeded.', 'wooagent'), array('status' => 429));
        }

        return true;
    }

    /**
     * Chat bridge endpoint.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function chat($request)
    {
        $session_id = sanitize_text_field((string) $request->get_param('session_id'));
        $message = sanitize_textarea_field((string) $request->get_param('message'));
        $message_type = sanitize_key((string) $request->get_param('message_type'));
        $history = $request->get_param('conversation_history');
        $cart_context = $request->get_param('cart_context');

        if ($session_id === '' || $message === '') {
            return $this->error_response('session_id and message are required.', 400);
        }

        if (!in_array($message_type, array('text', 'voice_transcript'), true)) {
            $message_type = 'text';
        }

        $options = get_option(WOOAGENT_OPTION_KEY, array());
        $backend_url = isset($options['backend_url']) ? esc_url_raw($options['backend_url']) : '';

        if ($backend_url === '') {
            return $this->error_response('Backend URL is not configured.', 500);
        }

        if ($this->is_invalid_backend_url($backend_url)) {
            return $this->error_response(
                'Invalid Backend URL. Use your FastAPI base URL (example: http://127.0.0.1:8000 or https://xxxx.ngrok-free.app). Do not use 0.0.0.0 or WordPress wp-json URLs.',
                500
            );
        }

        $payload = array(
            'session_id' => $session_id,
            'message' => $message,
            'message_type' => $message_type,
            'store_url' => home_url('/'),
            'store_name' => get_bloginfo('name'),
            'currency' => function_exists('get_woocommerce_currency_symbol') ? get_woocommerce_currency_symbol() : '$',
            'conversation_history' => is_array($history) ? array_slice($history, -10) : array(),
            'cart_context' => is_array($cart_context) ? $cart_context : $this->get_cart_context(),
        );

        $json_payload = wp_json_encode($payload);
        $timestamp    = (string) time();
        $chat_path    = '/chat';
        $signature    = $this->auth->sign_payload($json_payload, $timestamp, $chat_path);

        $response = wp_remote_post(
            trailingslashit($backend_url) . 'chat',
            array(
                'headers' => array(
                    'Content-Type' => 'application/json',
                    'X-WooAgent-Timestamp' => $timestamp,
                    'X-WooAgent-Signature' => $signature,
                ),
                'body' => $json_payload,
                'timeout' => 12,
            )
        );

        if (is_wp_error($response)) {
            return $this->error_response($response->get_error_message(), 502);
        }

        $status_code = wp_remote_retrieve_response_code($response);
        $body = wp_remote_retrieve_body($response);
        $data = json_decode($body, true);

        if ($status_code < 200 || $status_code >= 300) {
            $backend_error = 'Unable to process assistant response.';
            if (is_array($data) && isset($data['detail'])) {
                if (is_string($data['detail'])) {
                    $backend_error = $data['detail'];
                } elseif (is_array($data['detail'])) {
                    $backend_error = wp_json_encode($data['detail']);
                }
            } elseif (is_array($data) && isset($data['error']) && is_string($data['error'])) {
                $backend_error = $data['error'];
            } elseif (!empty($body) && is_string($body)) {
                $backend_error = wp_strip_all_tags($body);
            }

            return $this->error_response(
                sprintf('Backend request failed (HTTP %d): %s', (int) $status_code, $backend_error),
                502
            );
        }

        if (!is_array($data)) {
            return $this->error_response('Backend returned an invalid JSON payload.', 502);
        }

        $conversation = isset($payload['conversation_history']) && is_array($payload['conversation_history'])
            ? $payload['conversation_history']
            : array();
        $conversation[] = array('role' => 'user', 'content' => $message);
        $conversation[] = array('role' => 'assistant', 'content' => isset($data['response_text']) ? (string) $data['response_text'] : '');

        $this->persist_session($session_id, array_slice($conversation, -20), $this->get_cart_context());

        $result = array(
            'response_text' => isset($data['response_text']) ? $data['response_text'] : '',
            'response_audio_url' => isset($data['response_audio_url']) ? $data['response_audio_url'] : null,
            'actions_taken' => isset($data['actions']) ? $data['actions'] : array(),
            'cart_updated' => $this->get_cart_context(),
            'session_id' => isset($data['session_id']) ? $data['session_id'] : $session_id,
            'suggested_replies' => isset($data['suggested_replies']) ? $data['suggested_replies'] : array(),
        );

        return $this->success_response($result);
    }

    /**
     * Return basic session state.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function get_session($request)
    {
        global $wpdb;

        $session_id = sanitize_text_field((string) $request['session_id']);
        if ($session_id === '') {
            return $this->error_response('session_id is required.', 400);
        }

        $table_name = $wpdb->prefix . 'wooagent_sessions';
        $row = $wpdb->get_row(
            $wpdb->prepare("SELECT * FROM {$table_name} WHERE session_id = %s", $session_id),
            ARRAY_A
        );

        if (!$row) {
            return $this->success_response(array(
                'session_id' => $session_id,
                'conversation_summary' => '',
                'cart' => $this->get_cart_context(),
            ));
        }

        $history = json_decode((string) $row['conversation_history'], true);
        if (!is_array($history)) {
            $history = array();
        }

        $summary_lines = array();
        foreach (array_slice($history, -6) as $entry) {
            if (!is_array($entry)) {
                continue;
            }
            $role = isset($entry['role']) ? $entry['role'] : 'assistant';
            $content = isset($entry['content']) ? wp_strip_all_tags((string) $entry['content']) : '';
            if ($content !== '') {
                $summary_lines[] = ucfirst($role) . ': ' . $content;
            }
        }

        return $this->success_response(array(
            'session_id' => $session_id,
            'conversation_summary' => implode("\n", $summary_lines),
            'cart' => $this->get_cart_context(),
            'updated_at' => isset($row['updated_at']) ? $row['updated_at'] : '',
        ));
    }

    /**
     * Add product to cart.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function cart_add($request)
    {
        $this->ensure_cart_loaded();

        $product_id = absint($request->get_param('product_id'));
        $variation_id = absint($request->get_param('variation_id'));
        $quantity = max(1, absint($request->get_param('quantity')));
        $variation = $request->get_param('variation');
        $variation_data = array();

        if ($product_id <= 0) {
            return $this->error_response('product_id is required.', 400);
        }

        if (is_array($variation)) {
            foreach ($variation as $attr_key => $attr_value) {
                $key = sanitize_text_field((string) $attr_key);
                $value = sanitize_text_field((string) $attr_value);
                if ($key === '' || $value === '') {
                    continue;
                }
                if (strpos($key, 'attribute_') !== 0) {
                    $key = 'attribute_' . sanitize_title($key);
                }
                $variation_data[$key] = $value;
            }
        }

        $product_obj = wc_get_product($product_id);

        // If caller passed a variation as product_id, normalize to parent+variation payload.
        if ($product_obj instanceof WC_Product_Variation && $variation_id <= 0) {
            $variation_id = (int) $product_obj->get_id();
            $parent_id = (int) $product_obj->get_parent_id();
            if ($parent_id > 0) {
                $product_id = $parent_id;
            }
            if (empty($variation_data)) {
                $variation_data = (array) $product_obj->get_variation_attributes();
            }
            $product_obj = wc_get_product($product_id);
        }

        if ($product_obj instanceof WC_Product && $product_obj->is_type('variable') && $variation_id <= 0) {
            $selected = $this->pick_instock_variation($product_obj, $variation_data);
            if (!empty($selected['variation_id'])) {
                $variation_id = (int) $selected['variation_id'];
                if (!empty($selected['attributes']) && is_array($selected['attributes'])) {
                    $variation_data = array_merge($selected['attributes'], $variation_data);
                }
            }
        }

        if ($variation_id > 0) {
            $variation_product = wc_get_product($variation_id);
            if ($variation_product instanceof WC_Product_Variation) {
                $parent_id = $variation_product->get_parent_id();
                if ($parent_id > 0) {
                    $product_id = $parent_id;
                }

                if (empty($variation_data)) {
                    $variation_data = $variation_product->get_variation_attributes();
                }
            }
        }

        if (!WC()->cart) {
            return $this->error_response('Cart is not available. Please refresh the page and try again.', 503);
        }

        $added = WC()->cart->add_to_cart($product_id, $quantity, $variation_id, $variation_data);

        if (!$added && $variation_id > 0) {
            // Last fallback: add the variation as direct purchasable item when parent attribute validation blocks.
            $added = WC()->cart->add_to_cart($variation_id, $quantity);
        }

        if (!$added) {
            $notices = wc_get_notices('error');
            $error_message = __('Unable to add item to cart.', 'wooagent');
            if (!empty($notices) && is_array($notices)) {
                $last_notice = end($notices);
                if (is_array($last_notice) && isset($last_notice['notice'])) {
                    $error_message = wp_strip_all_tags((string) $last_notice['notice']);
                }
            }
            wc_clear_notices();
            return $this->error_response($error_message, 400);
        }

        // Force WooCommerce to save the cart data immediately avoiding stateless REST reset.
        WC()->cart->calculate_totals();
        if (WC()->session && method_exists(WC()->session, 'save_data')) {
            WC()->session->save_data();
        }
        // Re-issue session cookie after saving so new sessions are visible on page reload.
        if (WC()->session && method_exists(WC()->session, 'set_customer_session_cookie')) {
            WC()->session->set_customer_session_cookie(true);
        }

        return $this->success_response(array(
            'success' => true,
            'cart_count' => WC()->cart->get_cart_contents_count(),
            'cart_total' => WC()->cart->get_total('edit'),
            'message' => __('Item added to cart.', 'wooagent'),
            'cart' => $this->get_cart_context(),
        ));
    }

    /**
     * Pick an in-stock variation for variable products.
     *
     * @param WC_Product $product
     * @param array      $requested_attributes
     * @return array{variation_id:int,attributes:array}
     */
    private function pick_instock_variation($product, $requested_attributes = array())
    {
        if (!$product instanceof WC_Product || !$product->is_type('variable')) {
            return array('variation_id' => 0, 'attributes' => array());
        }

        $normalized_requested = array();
        foreach ((array) $requested_attributes as $key => $value) {
            $k = sanitize_text_field((string) $key);
            $v = sanitize_text_field((string) $value);
            if ($k === '' || $v === '') {
                continue;
            }
            if (strpos($k, 'attribute_') !== 0) {
                $k = 'attribute_' . sanitize_title($k);
            }
            $normalized_requested[$k] = $v;
        }

        $fallback = array('variation_id' => 0, 'attributes' => array());
        foreach ((array) $product->get_children() as $child_id) {
            $variation = wc_get_product($child_id);
            if (!$variation instanceof WC_Product_Variation) {
                continue;
            }
            if (!$variation->exists() || !$variation->is_purchasable() || !$variation->is_in_stock()) {
                continue;
            }

            $attrs = (array) $variation->get_variation_attributes();
            if ($fallback['variation_id'] === 0) {
                $fallback = array(
                    'variation_id' => (int) $variation->get_id(),
                    'attributes' => $attrs,
                );
            }

            if (empty($normalized_requested)) {
                continue;
            }

            $matches = true;
            foreach ($normalized_requested as $key => $value) {
                $candidate = isset($attrs[$key]) ? (string) $attrs[$key] : '';
                if ($candidate === '') {
                    continue;
                }
                if (sanitize_title($candidate) !== sanitize_title((string) $value)) {
                    $matches = false;
                    break;
                }
            }

            if ($matches) {
                return array(
                    'variation_id' => (int) $variation->get_id(),
                    'attributes' => $attrs,
                );
            }
        }

        return $fallback;
    }

    /**
     * Remove cart item.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function cart_remove($request)
    {
        $this->ensure_cart_loaded();

        $cart_item_key = sanitize_text_field((string) $request->get_param('cart_item_key'));
        if ($cart_item_key === '') {
            return $this->error_response('cart_item_key is required.', 400);
        }

        $removed = WC()->cart->remove_cart_item($cart_item_key);
        if (!$removed) {
            return $this->error_response('Unable to remove item from cart.', 400);
        }

        // Force WooCommerce to save the cart data immediately avoiding stateless REST reset.
        WC()->cart->calculate_totals();
        if (WC()->session && method_exists(WC()->session, 'save_data')) {
            WC()->session->save_data();
        }
        if (WC()->session && method_exists(WC()->session, 'set_customer_session_cookie')) {
            WC()->session->set_customer_session_cookie(true);
        }

        return $this->success_response(array(
            'success' => true,
            'cart_count' => WC()->cart->get_cart_contents_count(),
            'cart_total' => WC()->cart->get_total('edit'),
            'cart' => $this->get_cart_context(),
        ));
    }

    /**
     * Update quantity of a cart item.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function cart_update($request)
    {
        $this->ensure_cart_loaded();

        $product_id = absint($request->get_param('product_id'));
        $quantity = absint($request->get_param('quantity'));

        if ($product_id <= 0) {
            return $this->error_response('product_id is required.', 400);
        }

        $cart_item_key = '';
        foreach (WC()->cart->get_cart() as $key => $item) {
            if ($item['product_id'] == $product_id || $item['variation_id'] == $product_id) {
                $cart_item_key = $key;
                break;
            }
        }

        if ($cart_item_key === '') {
            return $this->error_response('Product not found in cart.', 404);
        }

        if ($quantity <= 0) {
            $updated = WC()->cart->remove_cart_item($cart_item_key);
        } else {
            $updated = WC()->cart->set_quantity($cart_item_key, $quantity, true);
        }

        if (!$updated) {
            return $this->error_response('Unable to update cart quantity.', 400);
        }

        WC()->cart->calculate_totals();
        if (WC()->session && method_exists(WC()->session, 'save_data')) {
            WC()->session->save_data();
        }

        return $this->success_response(array(
            'success' => true,
            'cart_count' => WC()->cart->get_cart_contents_count(),
            'cart_total' => WC()->cart->get_total('edit'),
            'cart' => $this->get_cart_context(),
        ));
    }

    /**
     * Get full cart payload.
     *
     * @return WP_REST_Response
     */
    public function cart_get()
    {
        $this->ensure_cart_loaded();
        return $this->success_response($this->get_cart_context());
    }

    /**
     * Product search endpoint.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function products_search($request)
    {
        $q = sanitize_text_field((string) $request->get_param('q'));
        $category = sanitize_text_field((string) $request->get_param('category'));
        $min_price = $request->get_param('min_price');
        $max_price = $request->get_param('max_price');
        $in_stock_only = wc_string_to_bool((string) $request->get_param('in_stock_only'));
        $per_page = absint($request->get_param('per_page'));

        if ($per_page <= 0) {
            $per_page = 10;
        }

        $meta_query = array();
        if ($min_price !== null && $min_price !== '') {
            $meta_query[] = array(
                'key' => '_price',
                'value' => floatval($min_price),
                'compare' => '>=',
                'type' => 'NUMERIC',
            );
        }

        if ($max_price !== null && $max_price !== '') {
            $meta_query[] = array(
                'key' => '_price',
                'value' => floatval($max_price),
                'compare' => '<=',
                'type' => 'NUMERIC',
            );
        }

        if ($in_stock_only) {
            $meta_query[] = array(
                'key' => '_stock_status',
                'value' => 'instock',
            );
        }

        $args = array(
            'status' => 'publish',
            'limit' => $per_page,
            'return' => 'objects',
            's' => $q,
            'meta_query' => $meta_query,
        );

        if ($category !== '') {
            $args['category'] = array($category);
        }

        $query = new WC_Product_Query($args);
        $products = $query->get_products();

        $results = array();
        foreach ($products as $product) {
            if (!$product instanceof WC_Product) {
                continue;
            }
            $results[] = $this->format_product_search_result($product);
        }

        return $this->success_response($results);
    }

    /**
     * Product detail endpoint.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function product_detail($request)
    {
        $product_id = absint($request['id']);
        $product = wc_get_product($product_id);

        if (!$product) {
            return $this->error_response('Product not found.', 404);
        }

        try {
            $variations = array();
            if ($product->is_type('variable')) {
                foreach ($product->get_children() as $child_id) {
                    $variation = wc_get_product($child_id);
                    if (!$variation) {
                        continue;
                    }

                    $var_image = '';
                    if ($variation->get_image_id()) {
                        $var_image = wp_get_attachment_image_url($variation->get_image_id(), 'woocommerce_thumbnail');
                    }

                    $variations[] = array(
                        'id' => $variation->get_id(),
                        'variation_id' => $variation->get_id(),
                        'price' => wc_format_decimal($variation->get_price(), 2),
                        'regular_price' => wc_format_decimal($variation->get_regular_price(), 2),
                        'sale_price' => wc_format_decimal($variation->get_sale_price(), 2),
                        'stock_status' => $variation->get_stock_status(),
                        'stock_quantity' => $variation->get_stock_quantity(),
                        'is_in_stock' => $variation->is_in_stock(),
                        'attributes' => $variation->get_attributes(),
                        'image_url' => $var_image ? $var_image : '',
                    );
                }
            }

            $reviews = 0;
            $avg_rating = '0';
            try {
                $reviews = wc_get_product_review_count($product_id);
                $avg_rating = $product->get_average_rating();
            } catch (\Exception $e) {
                // Ignore review errors
            }

            $categories = array();
            $terms = get_the_terms($product_id, 'product_cat');
            if (is_array($terms)) {
                foreach ($terms as $term) {
                    $categories[] = array(
                        'id' => $term->term_id,
                        'name' => $term->name,
                        'slug' => $term->slug,
                    );
                }
            }

            $related_products = array();
            foreach (array_slice(wc_get_related_products($product_id, 4), 0, 4) as $related_id) {
                $related = wc_get_product($related_id);
                if (!$related) {
                    continue;
                }
                $related_products[] = $this->format_product_search_result($related);
            }

            $images = array();
            if ($product->get_image_id()) {
                $images[] = wp_get_attachment_image_url($product->get_image_id(), 'large');
            }
            foreach ($product->get_gallery_image_ids() as $image_id) {
                $images[] = wp_get_attachment_image_url($image_id, 'large');
            }

            // Convert WC_Product_Attribute objects to plain arrays for JSON serialization
            $raw_attributes = $product->get_attributes();
            $plain_attributes = array();
            foreach ($raw_attributes as $attr_key => $attr) {
                if (is_object($attr) && method_exists($attr, 'get_data')) {
                    $plain_attributes[$attr_key] = $attr->get_data();
                } elseif (is_object($attr)) {
                    $plain_attributes[$attr_key] = (array) $attr;
                } else {
                    $plain_attributes[$attr_key] = $attr;
                }
            }

            // Main image URL for image_url field
            $main_image_url = '';
            if (!empty($images)) {
                $main_image_url = $images[0];
            }

            return $this->success_response(array(
                'id' => $product->get_id(),
                'name' => $product->get_name(),
                'description' => wp_kses_post($product->get_description()),
                'short_description' => wp_strip_all_tags($product->get_short_description()),
                'price' => $product->get_price(),
                'regular_price' => $product->get_regular_price(),
                'sale_price' => $product->get_sale_price(),
                'on_sale' => $product->is_on_sale(),
                'stock_quantity' => $product->get_stock_quantity(),
                'stock_status' => $product->get_stock_status(),
                'permalink' => $product->get_permalink(),
                'image_url' => $main_image_url ? $main_image_url : '',
                'images' => array_values(array_filter($images)),
                'attributes' => $plain_attributes,
                'variations' => $variations,
                'reviews_summary' => array(
                    'count' => $reviews,
                    'average_rating' => $avg_rating,
                ),
                'categories' => $categories,
                'related_products' => $related_products,
            ));
        } catch (\Exception $e) {
            return $this->error_response('Error loading product details: ' . $e->getMessage(), 500);
        } catch (\Error $e) {
            return $this->error_response('Error loading product details: ' . $e->getMessage(), 500);
        }
    }

    /**
     * Fetch last five orders by billing email.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function orders_by_email($request)
    {
        $email = sanitize_email(urldecode((string) $request['email']));
        if (!is_email($email)) {
            return $this->error_response('Valid email is required.', 400);
        }

        $orders = wc_get_orders(array(
            'billing_email' => $email,
            'limit' => 5,
            'orderby' => 'date',
            'order' => 'DESC',
        ));

        $result = array();
        foreach ($orders as $order) {
            if (!$order instanceof WC_Order) {
                continue;
            }

            $tracking = $order->get_meta('_tracking_number');
            if (!$tracking) {
                $tracking = $order->get_meta('tracking_number');
            }

            $items = array();
            foreach ($order->get_items() as $item) {
                $items[] = array(
                    'name' => $item->get_name(),
                    'quantity' => $item->get_quantity(),
                );
            }

            $result[] = array(
                'order_id' => $order->get_id(),
                'order_number' => $order->get_order_number(),
                'status' => $order->get_status(),
                'date_created' => $order->get_date_created() ? $order->get_date_created()->date('c') : '',
                'total' => $order->get_total(),
                'currency' => $order->get_currency(),
                'tracking_info' => $tracking ? $tracking : '',
                'items' => $items,
            );
        }

        return $this->success_response($result);
    }

    /**
     * Get variations for a product.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function product_variations($request)
    {
        $product_id = absint($request['id']);
        $product = wc_get_product($product_id);

        if (!$product) {
            return $this->error_response('Product not found.', 404);
        }

        if (!$product->is_type('variable')) {
            return $this->error_response('Product is not variable.', 400);
        }

        $variation_ids = $product->get_children();
        $variations = array();

        foreach ($variation_ids as $var_id) {
            $variation = wc_get_product($var_id);
            if (!$variation)
                continue;

            $variations[] = array(
                'id' => $variation->get_id(),
                'variation_id' => $variation->get_id(),
                'attributes' => $variation->get_attributes(),
                'price' => $variation->get_price(),
                'regular_price' => $variation->get_regular_price(),
                'sale_price' => $variation->get_sale_price(),
                'on_sale' => $variation->is_on_sale(),
                'stock_status' => $variation->get_stock_status(),
                'stock_quantity' => $variation->get_stock_quantity(),
                'is_in_stock' => $variation->is_in_stock(),
                'image_url' => wp_get_attachment_image_url($variation->get_image_id(), 'full'),
            );
        }

        return $this->success_response(array(
            'product_id' => $product_id,
            'variations' => $variations
        ));
    }

    /**
     * Submit a review for a product.
     *
     * @param WP_REST_Request $request
     * @return WP_REST_Response
     */
    public function product_review($request)
    {
        $product_id = absint($request['id']);
        $review = sanitize_textarea_field($request->get_param('review'));
        $rating = absint($request->get_param('rating'));
        $name = sanitize_text_field($request->get_param('name'));
        $email = sanitize_email($request->get_param('email'));

        if (!$product_id || !$review || !$rating) {
            return $this->error_response('Product ID, review text and rating (1-5) are required.', 400);
        }

        $data = array(
            'comment_post_ID' => $product_id,
            'comment_author' => $name ? $name : 'Customer',
            'comment_author_email' => $email ? $email : 'customer@example.com',
            'comment_content' => $review,
            'comment_type' => 'review',
            'comment_parent' => 0,
            'user_id' => get_current_user_id(),
            'comment_author_IP' => $_SERVER['REMOTE_ADDR'] ?? '',
            'comment_agent' => 'WooAgent',
            'comment_date' => current_time('mysql'),
            'comment_approved' => 1,
        );

        $comment_id = wp_insert_comment($data);

        if ($comment_id) {
            update_comment_meta($comment_id, 'rating', $rating);
            return $this->success_response(array(
                'success' => true,
                'comment_id' => $comment_id,
                'message' => 'Review submitted successfully.'
            ));
        }

        return $this->error_response('Failed to submit review.', 500);
    }

    /**
     * Build uniform success response.
     *
     * @param mixed $data
     * @param int $status
     * @return WP_REST_Response
     */
    private function success_response($data, $status = 200)
    {
        return new WP_REST_Response(array(
            'success' => true,
            'data' => $data,
            'error' => '',
        ), $status);
    }

    /**
     * Build uniform error response.
     *
     * @param string $error
     * @param int $status
     * @return WP_REST_Response
     */
    private function error_response($error, $status = 400)
    {
        return new WP_REST_Response(array(
            'success' => false,
            'data' => array(),
            'error' => $error,
        ), $status);
    }

    /**
     * Ensure cart object is available.
     */
    private function ensure_cart_loaded()
    {
        if (!function_exists('WC') || !WC()) {
            return;
        }

        // Always attempt to load the cart — wc_load_cart() is a no-op when already loaded.
        if (function_exists('wc_load_cart')) {
            wc_load_cart();
        }

        // Explicit get_cart_from_session fallback in case wc_load_cart skipped it.
        if (WC()->cart && method_exists(WC()->cart, 'get_cart_from_session')) {
            WC()->cart->get_cart_from_session();
        }

        if (WC()->session && method_exists(WC()->session, 'set_customer_session_cookie')) {
            WC()->session->set_customer_session_cookie(true);
        }
    }

    /**
     * Return serializable cart details.
     *
     * @return array
     */
    private function get_cart_context()
    {
        $this->ensure_cart_loaded();

        if (!function_exists('WC') || !WC() || !WC()->cart) {
            return array(
                'items' => array(),
                'count' => 0,
                'total' => '0',
            );
        }

        $items = array();
        foreach (WC()->cart->get_cart() as $cart_item_key => $item) {
            $product = isset($item['data']) ? $item['data'] : null;
            if (!$product instanceof WC_Product) {
                continue;
            }

            $items[] = array(
                'cart_item_key' => $cart_item_key,
                'product_id' => $product->get_id(),
                'variation_id' => isset($item['variation_id']) ? (int) $item['variation_id'] : 0,
                'name' => $product->get_name(),
                'quantity' => (int) $item['quantity'],
                'line_total' => wc_price((float) $item['line_total']),
                'price' => wc_price((float) wc_get_price_to_display($product)),
                'image_url' => wp_get_attachment_image_url($product->get_image_id(), 'thumbnail'),
                'permalink' => $product->get_permalink(),
            );
        }

        return array(
            'items' => $items,
            'count' => WC()->cart->get_cart_contents_count(),
            'total' => html_entity_decode(strip_tags(WC()->cart->get_cart_total())),
        );
    }

    /**
     * Build lightweight product object for search results.
     *
     * @param WC_Product $product
     * @return array
     */
    private function format_product_search_result($product)
    {
        $variations_summary = array();
        $fallback_variation_image = '';
        $any_variation_in_stock = false;

        if ($product->is_type('variable')) {
            foreach (array_slice($product->get_available_variations(), 0, 8) as $variation) {
                $variation_image = '';
                if (isset($variation['image']) && is_array($variation['image'])) {
                    if (!empty($variation['image']['src'])) {
                        $variation_image = $variation['image']['src'];
                    } elseif (!empty($variation['image']['url'])) {
                        $variation_image = $variation['image']['url'];
                    }
                }
                if ($variation_image === '' && !empty($variation['image_id'])) {
                    $variation_image = wp_get_attachment_image_url((int) $variation['image_id'], 'woocommerce_thumbnail');
                }
                if ($fallback_variation_image === '' && $variation_image) {
                    $fallback_variation_image = $variation_image;
                }
                if (!empty($variation['is_in_stock'])) {
                    $any_variation_in_stock = true;
                }

                $variations_summary[] = array(
                    'variation_id' => isset($variation['variation_id']) ? (int) $variation['variation_id'] : 0,
                    'attributes' => isset($variation['attributes']) ? $variation['attributes'] : array(),
                    'price_html' => isset($variation['price_html']) ? wp_strip_all_tags($variation['price_html']) : '',
                    'is_in_stock' => !empty($variation['is_in_stock']),
                    'image_url' => $variation_image ? $variation_image : '',
                    'stock_status' => !empty($variation['is_in_stock']) ? 'instock' : 'outofstock',
                );
            }
        }

        $image_url = wp_get_attachment_image_url($product->get_image_id(), 'woocommerce_thumbnail');
        if (!$image_url && $fallback_variation_image) {
            $image_url = $fallback_variation_image;
        }

        $stock_status = $product->get_stock_status();
        if ($product->is_type('variable') && $any_variation_in_stock) {
            $stock_status = 'instock';
        }

        return array(
            'id' => $product->get_id(),
            'name' => $product->get_name(),
            'price' => wc_format_decimal($product->get_regular_price() !== '' ? $product->get_regular_price() : $product->get_price(), 2),
            'sale_price' => wc_format_decimal($product->get_sale_price(), 2),
            'stock_status' => $stock_status,
            'stock_quantity' => $product->get_stock_quantity(),
            'image_url' => $image_url ? $image_url : '',
            'permalink' => $product->get_permalink(),
            'short_description' => wp_strip_all_tags($product->get_short_description()),
            'attributes' => $product->get_attributes(),
            'variations_summary' => $variations_summary,
        );
    }

    /**
     * Simple per-minute session limiter.
     *
     * @param string $session_id
     * @param string $route
     * @return bool
     */
    private function check_rate_limit($session_id, $route)
    {
        if ($session_id === '') {
            $session_id = $this->get_client_identifier($route);
        }

        $key = 'wooagent_rl_' . md5($session_id . '|' . gmdate('YmdHi'));
        $count = (int) get_transient($key);

        if ($count >= 30) {
            return false;
        }

        set_transient($key, $count + 1, 70);
        return true;
    }

    /**
     * Persist session row to database.
     *
     * @param string $session_id
     * @param array $conversation_history
     * @param array $cart_snapshot
     */
    private function persist_session($session_id, $conversation_history, $cart_snapshot)
    {
        global $wpdb;

        $table_name = $wpdb->prefix . 'wooagent_sessions';
        $now = current_time('mysql');

        $existing = $wpdb->get_var(
            $wpdb->prepare("SELECT id FROM {$table_name} WHERE session_id = %s", $session_id)
        );

        $data = array(
            'conversation_history' => wp_json_encode($conversation_history),
            'cart_snapshot' => wp_json_encode($cart_snapshot),
            'updated_at' => $now,
        );

        if ($existing) {
            $wpdb->update($table_name, $data, array('session_id' => $session_id));
            return;
        }

        $data['session_id'] = $session_id;
        $data['created_at'] = $now;
        $data['customer_email'] = null;

        $wpdb->insert($table_name, $data);
    }

    /**
     * Build fallback identifier for rate-limiter.
     *
     * @param string $route
     * @return string
     */
    private function get_client_identifier($route)
    {
        $ip = isset($_SERVER['REMOTE_ADDR']) ? sanitize_text_field(wp_unslash($_SERVER['REMOTE_ADDR'])) : 'unknown';
        return $route . '|' . $ip;
    }

    /**
     * Prevent recursive/misconfigured backend URLs that point to WP REST itself.
     *
     * @param string $backend_url
     * @return bool
     */
    private function is_invalid_backend_url($backend_url)
    {
        $normalized = untrailingslashit($backend_url);
        if (strpos($normalized, '/wp-json/') !== false) {
            return true;
        }

        $backend_host = wp_parse_url($normalized, PHP_URL_HOST);
        if (strtolower((string) $backend_host) === '0.0.0.0') {
            return true;
        }
        $site_host = wp_parse_url(home_url('/'), PHP_URL_HOST);
        $backend_port = wp_parse_url($normalized, PHP_URL_PORT);
        $site_port = wp_parse_url(home_url('/'), PHP_URL_PORT);

        if ($site_port === null) {
            $site_scheme = wp_parse_url(home_url('/'), PHP_URL_SCHEME);
            $site_port = ($site_scheme === 'https') ? 443 : 80;
        }
        if ($backend_port === null) {
            $backend_scheme = wp_parse_url($normalized, PHP_URL_SCHEME);
            $backend_port = ($backend_scheme === 'https') ? 443 : 80;
        }

        if (!empty($backend_host) && !empty($site_host) && strtolower($backend_host) === strtolower($site_host)) {
            $path = (string) wp_parse_url($normalized, PHP_URL_PATH);
            // Prevent WordPress calling itself for /chat, which can deadlock and time out.
            if ((string) $backend_port === (string) $site_port && ($path === '' || $path === '/')) {
                return true;
            }
            if (strpos($path, '/wp-json') === 0 || strpos($path, '/xmlrpc.php') === 0) {
                return true;
            }
        }

        return false;
    }
}
