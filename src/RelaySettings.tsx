import { FC, useCallback, useEffect, useState } from 'react';
import { ButtonItem, DropdownItem, Field, PanelSectionRow, TextField } from '@decky/ui';
import { callable } from '@decky/api';

type Mode = 'auto' | 'local' | 'remote';

interface RelaySettingsValues {
    mode: Mode;
    target: string;
    token: string; // "set" when a token is stored; the secret itself never comes back
    fingerprint: string;
    socket: string;
    error?: string;
}

const getRelaySettings = callable<[], RelaySettingsValues>('get_relay_settings');
const setRelaySettings = callable<[Partial<RelaySettingsValues>], RelaySettingsValues>(
    'set_relay_settings'
);
const getRelayStatus = callable<[], string>('get_relay_status');

const MODES: { data: Mode; label: string }[] = [
    { data: 'auto', label: 'Automatic: this device first, then the target' },
    { data: 'local', label: 'Discord on this device only' },
    { data: 'remote', label: 'Another machine only' }
];

/** Where statuses go: the Discord app on this device, or a receiver on another machine. */
const RelaySettings: FC<{}> = () => {
    const [values, setValues] = useState<RelaySettingsValues | null>(null);
    const [target, setTarget] = useState('');
    const [token, setToken] = useState('');
    const [fingerprint, setFingerprint] = useState('');
    const [status, setStatus] = useState('');
    const [error, setError] = useState('');

    const apply = useCallback((v: RelaySettingsValues) => {
        setValues(v);
        setTarget(v.target);
        setFingerprint(v.fingerprint);
        setError(v.error || '');
    }, []);

    useEffect(() => {
        getRelaySettings().then(apply);
        const refresh = async () => setStatus(await getRelayStatus());
        refresh();
        const timer = window.setInterval(refresh, 2000);
        return () => window.clearInterval(timer);
    }, [apply]);

    const save = useCallback(
        async (change: Partial<RelaySettingsValues>) => apply(await setRelaySettings(change)),
        [apply]
    );

    if (!values) {
        return null;
    }

    const usesTls = /^(tls|https|wss):\/\//i.test(target.trim());

    return (
        <>
            <PanelSectionRow>
                <Field label="Relay" description={status} bottomSeparator="none" />
            </PanelSectionRow>
            <PanelSectionRow>
                <DropdownItem
                    label="Send statuses to"
                    layout="below"
                    rgOptions={MODES}
                    selectedOption={values.mode}
                    onChange={(option) => save({ mode: option.data as Mode })}
                />
            </PanelSectionRow>
            {values.mode !== 'local' && (
                <>
                    <PanelSectionRow>
                        <TextField
                            label="Target"
                            description={
                                error ||
                                'Machine running Discord and a receiver: host, host:port (default port 8920), tls://host:port, http://host:port/path or https://host/path.'
                            }
                            value={target}
                            onChange={(e) => setTarget(e.target.value)}
                            onBlur={() => target.trim() !== values.target && save({ target })}
                        />
                    </PanelSectionRow>
                    <PanelSectionRow>
                        <TextField
                            label="Token"
                            description={
                                values.token
                                    ? 'A token is saved. Type a new one to replace it.'
                                    : 'Optional. Must match the token set on the receiver.'
                            }
                            bIsPassword={true}
                            value={token}
                            onChange={(e) => setToken(e.target.value)}
                            onBlur={() => {
                                if (token !== '') {
                                    save({ token });
                                    setToken('');
                                }
                            }}
                        />
                    </PanelSectionRow>
                    {values.token && (
                        <PanelSectionRow>
                            <ButtonItem layout="below" onClick={() => save({ token: '' })}>
                                Remove saved token
                            </ButtonItem>
                        </PanelSectionRow>
                    )}
                    {(usesTls || values.fingerprint) && (
                        <PanelSectionRow>
                            <TextField
                                label="Certificate fingerprint"
                                description="For a self-signed certificate: the SHA-256 fingerprint the receiver prints at start. Leave blank to verify normally."
                                value={fingerprint}
                                onChange={(e) => setFingerprint(e.target.value)}
                                onBlur={() =>
                                    fingerprint.trim() !== values.fingerprint && save({ fingerprint })
                                }
                                bShowClearAction={true}
                            />
                        </PanelSectionRow>
                    )}
                </>
            )}
        </>
    );
};

export default RelaySettings;
