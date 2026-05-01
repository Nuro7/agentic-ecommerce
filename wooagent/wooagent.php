<?php
/**
 * Plugin Name: WooAgent - AI Shopping Assistant
 * Plugin URI: https://example.com/wooagent
 * Description: Voice-first AI shopping assistant for WooCommerce stores.
 * Version: 1.4.31
 * Author: WooAgent Team
 * Author URI: https://example.com
 * Requires at least: 6.0
 * Requires PHP: 7.4
 * Text Domain: wooagent
 */

if (!defined('ABSPATH')) {
    exit;
}

define('WOOAGENT_VERSION', '1.4.31');
define('WOOAGENT_PLUGIN_FILE', __FILE__);
define('WOOAGENT_PLUGIN_DIR', plugin_dir_path(__FILE__));
define('WOOAGENT_PLUGIN_URL', plugin_dir_url(__FILE__));
define('WOOAGENT_OPTION_KEY', 'wooagent_settings');

require_once WOOAGENT_PLUGIN_DIR . 'includes/class-wooagent-auth.php';
require_once WOOAGENT_PLUGIN_DIR . 'includes/class-wooagent-settings.php';
require_once WOOAGENT_PLUGIN_DIR . 'includes/class-wooagent-api.php';

class WooAgent_Plugin
{
    /** @var WooAgent_Settings */
    private $settings;

    /** @var WooAgent_API */
    private $api;

    /**
     * Boot plugin hooks.
     */
    public function boot()
    {
        register_activation_hook(WOOAGENT_PLUGIN_FILE, array($this, 'activate'));
        register_deactivation_hook(WOOAGENT_PLUGIN_FILE, array($this, 'deactivate'));

        add_action('plugins_loaded', array($this, 'init'), 5);
        add_action('admin_notices', array($this, 'woocommerce_notice'));
        add_action('wp_enqueue_scripts', array($this, 'enqueue_widget_assets'));
        // Priority 20 — ensures WC core (priority 10) fully initialises its session before we load the cart.
        add_action('woocommerce_init', array($this, 'preload_cart_for_rest_cart'), 20);
    }

    /**
     * Pre-load the WooCommerce cart during woocommerce_init for REST cart endpoint
     * requests. This runs after WC_Session_Handler has read the browser's session
     * cookie, so wc_load_cart() loads the correct customer session — not a fresh one.
     */
    public function preload_cart_for_rest_cart()
    {
        if (!defined('REST_REQUEST') || !REST_REQUEST) {
            return;
        }

        $uri = isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '';
        if (strpos($uri, '/wooagent/v1/cart') === false) {
            return;
        }

        if (!function_exists('wc_load_cart') || !function_exists('WC')) {
            return;
        }

        wc_load_cart();

        if (WC()->session && method_exists(WC()->session, 'set_customer_session_cookie')) {
            WC()->session->set_customer_session_cookie(true);
        }
    }

    /**
     * Ensure required DB table exists.
     */
    public function activate()
    {
        global $wpdb;

        $table_name = $wpdb->prefix . 'wooagent_sessions';
        $charset_collate = $wpdb->get_charset_collate();

        $sql = "CREATE TABLE {$table_name} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            session_id VARCHAR(64) NOT NULL,
            customer_email VARCHAR(255) NULL,
            conversation_history LONGTEXT NULL,
            cart_snapshot LONGTEXT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY session_id (session_id)
        ) {$charset_collate};";

        require_once ABSPATH . 'wp-admin/includes/upgrade.php';
        dbDelta($sql);
    }

    /**
     * Deactivation hook.
     */
    public function deactivate()
    {
        // Leave data in place for analytics/auditing. Future cleanup can be configurable.
    }

    /**
     * Initialize plugin internals.
     */
    public function init()
    {
        if (!$this->is_woocommerce_active()) {
            return;
        }

        $this->settings = new WooAgent_Settings();
        $this->settings->init();

        $auth = new WooAgent_Auth();
        $this->api = new WooAgent_API($auth);
        $this->api->init();
    }

    /**
     * WooCommerce admin notice.
     */
    public function woocommerce_notice()
    {
        if (!current_user_can('activate_plugins')) {
            return;
        }

        if ($this->is_woocommerce_active()) {
            return;
        }

        echo '<div class="notice notice-error"><p>';
        echo esc_html__('WooAgent requires WooCommerce to be installed and active.', 'wooagent');
        echo '</p></div>';
    }

    /**
     * Enqueue frontend widget assets.
     */
    public function enqueue_widget_assets()
    {
        if (is_admin() || !$this->is_woocommerce_active()) {
            return;
        }

        $options = get_option(WOOAGENT_OPTION_KEY, array());

        wp_enqueue_style(
            'wooagent-widget',
            WOOAGENT_PLUGIN_URL . 'widget/wooagent-widget.css',
            array(),
            WOOAGENT_VERSION
        );

        wp_enqueue_script(
            'wooagent-widget',
            WOOAGENT_PLUGIN_URL . 'widget/wooagent-widget.js',
            array(),
            WOOAGENT_VERSION,
            true
        );

        wp_localize_script(
            'wooagent-widget',
            'wooagent_config',
            array(
                'ajax_url' => admin_url('admin-ajax.php'),
                'rest_url' => esc_url_raw(rest_url('wooagent/v1')),
                'nonce' => wp_create_nonce('wooagent_nonce'),
                'wp_rest_nonce' => wp_create_nonce('wp_rest'),
                'agent_api_url' => isset($options['backend_url']) ? esc_url_raw($options['backend_url']) : '',
                'store_name' => get_bloginfo('name'),
                'currency' => function_exists('get_woocommerce_currency_symbol') ? get_woocommerce_currency_symbol() : '$',
                'language' => substr(strtolower(get_locale()), 0, 2),
                'widget_position' => isset($options['widget_position']) ? sanitize_text_field($options['widget_position']) : 'bottom-right',
                'primary_color' => isset($options['primary_color']) ? sanitize_hex_color($options['primary_color']) : '#6366f1',
                'greeting_message' => isset($options['greeting_message']) ? sanitize_text_field($options['greeting_message']) : __('Hi! I\'m your shopping assistant. Ask me anything.', 'wooagent'),
                'enable_voice' => isset($options['enable_voice']) ? (bool) $options['enable_voice'] : true,
                'enable_text' => isset($options['enable_text']) ? (bool) $options['enable_text'] : true,
                'auto_open_mobile' => isset($options['auto_open_mobile']) ? (bool) $options['auto_open_mobile'] : false,
                'excluded_pages' => isset($options['excluded_pages']) ? sanitize_textarea_field($options['excluded_pages']) : '',
            )
        );
    }

    /**
     * Detect whether WooCommerce is active.
     *
     * @return bool
     */
    private function is_woocommerce_active()
    {
        return class_exists('WooCommerce');
    }
}

$wooagent_plugin = new WooAgent_Plugin();
$wooagent_plugin->boot();
