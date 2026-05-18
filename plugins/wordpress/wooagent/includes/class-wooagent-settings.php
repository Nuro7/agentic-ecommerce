<?php

if (! defined('ABSPATH')) {
    exit;
}

class WooAgent_Settings {
    const PAGE_SLUG = 'wooagent-settings';

    /**
     * Register hooks.
     */
    public function init() {
        add_action('admin_menu', array($this, 'register_menu'));
        add_action('admin_init', array($this, 'register_settings'));
        add_action('admin_enqueue_scripts', array($this, 'enqueue_assets'));
        add_action('wp_ajax_wooagent_test_connection', array($this, 'ajax_test_connection'));
    }

    /**
     * Add WooAgent submenu under WooCommerce.
     */
    public function register_menu() {
        add_submenu_page(
            'woocommerce',
            __('WooAgent Settings', 'wooagent'),
            __('WooAgent', 'wooagent'),
            'manage_woocommerce',
            self::PAGE_SLUG,
            array($this, 'render_page')
        );
    }

    /**
     * Register plugin options.
     */
    public function register_settings() {
        register_setting(
            'wooagent_settings_group',
            WOOAGENT_OPTION_KEY,
            array($this, 'sanitize_settings')
        );

        add_settings_section(
            'wooagent_main',
            __('Assistant Configuration', 'wooagent'),
            '__return_empty_string',
            self::PAGE_SLUG
        );

        $fields = array(
            'backend_url'      => __('Agent Backend URL', 'wooagent'),
            'api_secret'       => __('API Secret Key', 'wooagent'),
            'widget_position'  => __('Widget Position', 'wooagent'),
            'primary_color'    => __('Primary Color', 'wooagent'),
            'greeting_message' => __('Greeting Message', 'wooagent'),
            'enable_voice'     => __('Enable Voice Input', 'wooagent'),
            'enable_text'      => __('Enable Text Fallback', 'wooagent'),
            'auto_open_mobile' => __('Auto-open on mobile', 'wooagent'),
            'excluded_pages'   => __('Excluded Pages', 'wooagent'),
        );

        foreach ($fields as $field => $label) {
            add_settings_field(
                $field,
                $label,
                array($this, 'render_field'),
                self::PAGE_SLUG,
                'wooagent_main',
                array('field' => $field)
            );
        }
    }

    /**
     * Sanitize options.
     *
     * @param array $input
     * @return array
     */
    public function sanitize_settings($input) {
        $output = array();

        $output['backend_url'] = isset($input['backend_url']) ? esc_url_raw(trim($input['backend_url'])) : '';
        $output['api_secret'] = isset($input['api_secret']) ? sanitize_text_field($input['api_secret']) : '';

        $output['widget_position'] = isset($input['widget_position']) && in_array($input['widget_position'], array('bottom-right', 'bottom-left'), true)
            ? $input['widget_position']
            : 'bottom-right';

        $color = isset($input['primary_color']) ? sanitize_hex_color($input['primary_color']) : '#6366f1';
        $output['primary_color'] = $color ? $color : '#6366f1';

        $output['greeting_message'] = isset($input['greeting_message']) ? sanitize_text_field($input['greeting_message']) : '';
        $output['enable_voice'] = ! empty($input['enable_voice']) ? 1 : 0;
        $output['enable_text'] = ! empty($input['enable_text']) ? 1 : 0;
        $output['auto_open_mobile'] = ! empty($input['auto_open_mobile']) ? 1 : 0;

        $output['excluded_pages'] = isset($input['excluded_pages']) ? sanitize_textarea_field($input['excluded_pages']) : '';

        if (empty($output['backend_url'])) {
            add_settings_error(WOOAGENT_OPTION_KEY, 'backend_url', __('Agent Backend URL is required.', 'wooagent'), 'error');
        }

        return $output;
    }

    /**
     * Render setting field.
     *
     * @param array $args
     */
    public function render_field($args) {
        $field = $args['field'];
        $options = get_option(WOOAGENT_OPTION_KEY, array());
        $value = isset($options[$field]) ? $options[$field] : '';

        switch ($field) {
            case 'backend_url':
                printf(
                    '<input type="url" class="regular-text" name="%1$s[%2$s]" value="%3$s" required />',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    esc_attr($value)
                );
                echo '<p class="description">' . esc_html__('Example: https://agent.example.com', 'wooagent') . '</p>';
                break;

            case 'api_secret':
                printf(
                    '<input type="password" class="regular-text" name="%1$s[%2$s]" value="%3$s" autocomplete="new-password" />',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    esc_attr($value)
                );
                break;

            case 'widget_position':
                $positions = array(
                    'bottom-right' => __('Bottom Right', 'wooagent'),
                    'bottom-left'  => __('Bottom Left', 'wooagent'),
                );
                echo '<select name="' . esc_attr(WOOAGENT_OPTION_KEY) . '[' . esc_attr($field) . ']">';
                foreach ($positions as $position => $label) {
                    printf(
                        '<option value="%1$s" %2$s>%3$s</option>',
                        esc_attr($position),
                        selected($value, $position, false),
                        esc_html($label)
                    );
                }
                echo '</select>';
                break;

            case 'primary_color':
                printf(
                    '<input type="color" name="%1$s[%2$s]" value="%3$s" />',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    esc_attr($value ? $value : '#6366f1')
                );
                break;

            case 'greeting_message':
                printf(
                    '<input type="text" class="regular-text" name="%1$s[%2$s]" value="%3$s" maxlength="250" />',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    esc_attr($value)
                );
                break;

            case 'excluded_pages':
                printf(
                    '<textarea name="%1$s[%2$s]" rows="5" class="large-text">%3$s</textarea>',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    esc_textarea($value)
                );
                echo '<p class="description">' . esc_html__('One URL or path per line (for example: /checkout).', 'wooagent') . '</p>';
                break;

            case 'enable_voice':
            case 'enable_text':
            case 'auto_open_mobile':
                printf(
                    '<label><input type="checkbox" name="%1$s[%2$s]" value="1" %3$s /> %4$s</label>',
                    esc_attr(WOOAGENT_OPTION_KEY),
                    esc_attr($field),
                    checked((int) $value, 1, false),
                    esc_html__('Enabled', 'wooagent')
                );
                break;
        }
    }

    /**
     * Render settings page view.
     */
    public function render_page() {
        $options = get_option(WOOAGENT_OPTION_KEY, array());
        include WOOAGENT_PLUGIN_DIR . 'admin/admin-page.php';
    }

    /**
     * Load admin assets.
     *
     * @param string $hook
     */
    public function enqueue_assets($hook) {
        if (strpos($hook, self::PAGE_SLUG) === false) {
            return;
        }

        wp_enqueue_style(
            'wooagent-admin',
            WOOAGENT_PLUGIN_URL . 'admin/admin-styles.css',
            array(),
            WOOAGENT_VERSION
        );
    }

    /**
     * AJAX connection test for /health endpoint.
     */
    public function ajax_test_connection() {
        if (! current_user_can('manage_woocommerce')) {
            wp_send_json_error(array('message' => __('Unauthorized', 'wooagent')), 403);
        }

        check_ajax_referer('wooagent_test_connection', 'nonce');

        $options = get_option(WOOAGENT_OPTION_KEY, array());
        $backend_url = isset($options['backend_url']) ? esc_url_raw($options['backend_url']) : '';

        if (empty($backend_url)) {
            wp_send_json_error(array('message' => __('Backend URL is not configured.', 'wooagent')), 400);
        }

        $health_url = trailingslashit($backend_url) . 'health';
        $response = wp_remote_get($health_url, array('timeout' => 10));

        if (is_wp_error($response)) {
            wp_send_json_error(array('message' => $response->get_error_message()), 500);
        }

        $status_code = wp_remote_retrieve_response_code($response);
        $body = wp_remote_retrieve_body($response);

        if ($status_code < 200 || $status_code >= 300) {
            wp_send_json_error(
                array('message' => sprintf(__('Health check failed: HTTP %d', 'wooagent'), $status_code)),
                500
            );
        }

        $decoded = json_decode($body, true);
        if (! is_array($decoded) || ! isset($decoded['status']) || $decoded['status'] !== 'ok') {
            wp_send_json_error(
                array(
                    'message' => __('Health endpoint did not return expected backend payload. Ensure Backend URL points to FastAPI base URL (e.g. http://127.0.0.1:8000).', 'wooagent'),
                ),
                500
            );
        }

        wp_send_json_success(
            array(
                'message' => __('Connection successful.', 'wooagent'),
                'body'    => $body,
            )
        );
    }
}
