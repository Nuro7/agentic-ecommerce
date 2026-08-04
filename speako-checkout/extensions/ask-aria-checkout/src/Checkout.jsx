import {
  reactExtension,
  Banner,
  Button,
  BlockStack,
  Text,
  TextField,
  useExtensionApi,
  useApplyShippingAddressChange,
} from '@shopify/ui-extensions-react/checkout';
import { useState } from 'react';

const CONFIG = {
  backendUrl: 'https://speako.nuro7.in',
  tenantId: 'bigb-pisar0or.myshopify.com',
  tenantType: 'shop',
};

const $SESSION = 'wa_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);

export default reactExtension('purchase.checkout.block.render', () => <Extension />);

function toShopifyAddress(addr) {
  const a = addr && typeof addr === 'object' ? addr : {};
  const out = {};
  if (a.first_name) out.firstName = a.first_name;
  if (a.last_name) out.lastName = a.last_name;
  if (a.address_1 || a.address_line1) out.address1 = a.address_1 || a.address_line1;
  if (a.city) out.city = a.city;
  if (a.state_code || a.province || a.state) out.provinceCode = a.state_code || a.province || a.state;
  if (a.postcode || a.zip) out.zip = a.postcode || a.zip;
  if (a.country_code || a.country) out.countryCode = a.country_code || a.country;
  if (a.phone) out.phone = a.phone;
  return out;
}

function addressFromUiAction(action) {
  if (!action) return null;
  const payload = action.payload && typeof action.payload === 'object' ? action.payload : {};
  if (action.type === 'redirect_checkout_with_address') {
    return payload.shipping || payload.billing || payload;
  }
  return payload;
}

function Extension() {
  const { shop, instructions } = useExtensionApi();
  const applyAddressChange = useApplyShippingAddressChange();
  const [isOpen, setIsOpen] = useState(false);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [prefill, setPrefill] = useState('');
  const [chat, setChat] = useState([
    { sender: 'speako', text: 'Hi! I am Speako. Need help completing your order?' },
  ]);

  const applyPrefill = async (action) => {
    const address = toShopifyAddress(addressFromUiAction(action));
    if (!Object.keys(address).length) return;

    if (!applyAddressChange) {
      console.warn('[Speako checkout] applyShippingAddressChange unavailable');
      return;
    }
    if (instructions?.delivery?.canSelectCustomAddress === false) {
      console.warn('[Speako checkout] canSelectCustomAddress=false; skipping prefill');
      setPrefill('Address prefill blocked by checkout settings.');
      return;
    }

    try {
      const result = await applyAddressChange({ type: 'updateShippingAddress', address });
      if (result?.type === 'error') {
        console.error('[Speako checkout] address change failed:', result?.errors || result);
        setPrefill('Could not prefill your shipping address. Please enter it manually.');
      } else {
        setPrefill('Shipping address pre-filled from your conversation.');
      }
    } catch (err) {
      console.error('[Speako checkout] address change threw:', err);
      setPrefill('Could not prefill your shipping address. Please enter it manually.');
    }
  };

  const handleSend = async () => {
    const q = message.trim();
    if (!q || busy) return;

    setMessage('');
    setChat((prev) => [...prev, { sender: 'user', text: q }]);
    setBusy(true);

    let reply = 'Sorry, I had trouble reaching the server. Please try again.';
    try {
      const shopDomain = shop?.myshopifyDomain || CONFIG.tenantId;
      const url = `${CONFIG.backendUrl}/api/v1/chat?shop=${encodeURIComponent(shopDomain)}`;

      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          session_id: $SESSION,
          message: q,
          language: 'auto',
          store_name: shop?.name || 'the store',
          store_url: shop?.storefrontUrl || '',
        }),
      });

      const data = res.ok ? await res.json() : null;
      reply = data?.response_text || data?.text || reply;

      const action =
        data?.ui_action ||
        (Array.isArray(data?.ui_actions) && data.ui_actions[0]) ||
        (Array.isArray(data?.actions) && data.actions[0]) ||
        null;
      if (action) await applyPrefill(action);
    } catch (err) {
      console.error('[Speako checkout] chat failed:', err);
    }

    setChat((prev) => [...prev, { sender: 'speako', text: reply }]);
    setBusy(false);
  };

  return (
    <BlockStack spacing="base">
      <Button onPress={() => setIsOpen(!isOpen)}>
        {isOpen ? 'Close Speako Assistant' : '💬 Ask Speako for Help'}
      </Button>

      {isOpen && (
        <Banner title="Speako AI Assistant">
          <BlockStack spacing="base">
            {chat.map((msg, idx) => {
              const label = msg.sender === 'speako' ? '★ Speako: ' : 'You: ';
              return (
                <Text key={idx} appearance={msg.sender === 'speako' ? 'info' : 'subdued'}>
                  {label + msg.text}
                </Text>
              );
            })}

            {prefill ? <Text appearance="success">{prefill}</Text> : null}

            <TextField
              label="Type your question..."
              value={message}
              onChange={(val) => setMessage(val)}
            />
            <Button onPress={handleSend} disabled={busy || !message.trim()}>
              {busy ? 'Speako is thinking…' : 'Send'}
            </Button>
          </BlockStack>
        </Banner>
      )}
    </BlockStack>
  );
}
